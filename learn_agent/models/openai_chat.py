"""Chat Completions 适配器：编码、单次请求与解码；支持文本和函数工具。"""

import json
from copy import deepcopy
from typing import Any, cast

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, omit
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)

from ..tools import ToolCall, ToolResult
from .contracts import (
    ModelError,
    ModelMessage,
    ModelProtocolError,
    ModelRequest,
    ModelResponse,
    ProviderContinuation,
    TextMessage,
    TokenUsage,
)


def encode_message(
    message: ModelMessage, namespace: str | None = None
) -> ChatCompletionMessageParam:
    """供应商消息格式只存在于适配器，Runner 保存中立的消息对象。"""
    if isinstance(message, TextMessage):
        if message.role == "system":
            return {"role": "system", "content": message.content}
        return {"role": "user", "content": message.content}
    if isinstance(message, ToolResult):
        return {
            "role": "tool",
            "tool_call_id": message.call_id,
            "content": json.dumps(message.content, ensure_ascii=False),
        }
    if message.continuation is not None:
        if namespace is not None and message.continuation.namespace != namespace:
            raise ModelProtocolError("原生续接材料不属于当前模型端点。")
        # 原生 assistant 字典由解码器保存；保留供应商附加的续接字段。
        return cast(ChatCompletionAssistantMessageParam, deepcopy(message.continuation.payload))
    assistant: ChatCompletionAssistantMessageParam = {
        "role": "assistant",
        "content": message.text,
    }
    if message.tool_calls:
        assistant["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    if message.refusal is not None:
        assistant["refusal"] = message.refusal
    return assistant


def encode_input(
    request: ModelRequest, namespace: str | None = None
) -> tuple[list[ChatCompletionMessageParam], list[ChatCompletionToolParam]]:
    """发送与离线计量共用编码，避免遗漏工具 Schema 或原生续接字段。"""
    definitions: list[ChatCompletionToolParam] = [
        {
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        }
        for spec in request.tools
    ]
    messages = [encode_message(message, namespace) for message in request.messages]
    return messages, definitions


def estimate_input_tokens(request: ModelRequest) -> int:
    """以完整输入 JSON 的 UTF-8 字节数保守估算 Token，不冒充供应商精确值。

    每字节计一个估算单位，包含角色、调用参数、Schema 与续接数据。
    服务端聊天模板开销由应用安全余量覆盖；更换协议时须更换计量器。
    """
    messages, definitions = encode_input(request)
    payload: dict[str, Any] = {"messages": messages}
    if definitions:
        payload.update(tools=definitions, parallel_tool_calls=False)
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


class OpenAIChatModel:
    """封装 SDK 协议；client 的配置与生命周期由LLMService 负责。"""

    def __init__(
        self, client: OpenAI, model: str, *, extra_body: dict[str, Any] | None = None
    ) -> None:
        # 一次 invoke 对应一次网络尝试，不能叠加 SDK 默认重试。
        self._client = client.with_options(max_retries=0)
        self._model = model
        self._extra_body = deepcopy(extra_body)
        # 原生材料只在相同协议、端点和模型之间续接，不跨模型静默复用。
        self._namespace = f"openai.chat.completions:{client.base_url}:{model}"

    def _decode(self, response: ChatCompletion) -> ModelResponse:
        """保留调用身份和原始参数；参数 JSON 校验留给工具系统执行。"""
        if not response.choices:
            raise ModelProtocolError("模型响应没有候选回复。")
        choice = response.choices[0]
        if choice.finish_reason not in ("stop", "tool_calls", "length", "content_filter"):
            raise ModelProtocolError("模型返回了不支持的停止原因。")
        message = choice.message
        if message.audio is not None or message.function_call is not None or message.annotations:
            raise ModelProtocolError("当前模型适配器只支持文本与函数工具调用。")
        calls = []
        for call in message.tool_calls or []:
            if call.type != "function":
                raise ModelProtocolError("只支持函数工具。")
            calls.append(ToolCall(call.id, call.function.name, call.function.arguments))
        if choice.finish_reason == "tool_calls" and not calls:
            raise ModelProtocolError("工具调用响应中没有工具调用。")
        usage = response.usage
        continuation = message.model_dump(mode="json", exclude_none=True)
        # annotations 是响应元数据，不属于 assistant 输入；当前也不支持引用输出。
        continuation.pop("annotations", None)
        return ModelResponse(
            text=message.content,
            tool_calls=tuple(calls),
            stop_reason="refusal" if message.refusal else choice.finish_reason,
            model=response.model,
            usage=(
                TokenUsage(usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
                if usage is not None
                else None
            ),
            refusal=message.refusal,
            continuation=ProviderContinuation(self._namespace, continuation),
        )

    def invoke(self, request: ModelRequest) -> ModelResponse:
        """本阶段不自动重试或切换端点；失败明确向 Runner 报告。"""
        messages, definitions = encode_input(request, self._namespace)
        try:
            # 无工具时省略工具参数；普通文本与工具请求共用同一条发送路径。
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=definitions or omit,
                parallel_tool_calls=False if definitions else omit,
                max_tokens=request.max_output_tokens,
                extra_body=self._extra_body,
            )
        except APITimeoutError as exc:
            raise ModelError("timeout", "模型请求超时。") from exc
        except APIConnectionError as exc:
            raise ModelError("connection_error", "模型连接失败。") from exc
        except APIStatusError as exc:
            if exc.status_code in (401, 403):
                code = "authentication_error"
            elif exc.status_code == 429:
                code = "rate_limit"
            elif exc.status_code >= 500:
                code = "server_error"
            else:
                code = "request_error"
            raise ModelError(
                code,
                f"模型请求失败（HTTP {exc.status_code}）。",
                status_code=exc.status_code,
                request_id=exc.request_id,
            ) from exc
        return self._decode(response)
