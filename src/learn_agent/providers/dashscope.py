"""DashScope's OpenAI-compatible Chat Completions protocol."""

from typing import Any

from learn_agent.contracts import Context, ModelError, Reply, ToolCall


def chat_messages(context: Context) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": context.instructions}]
    for message in context.messages:
        if message.role == "user":
            messages.append({"role": "user", "content": message.text})
        elif message.role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": message.text or None}
            if message.calls:
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in message.calls
                ]
            messages.append(item)
        elif message.result:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": message.call_id,
                    "content": message.result.model_dump_json(),
                }
            )
    return messages


def parse_completion(completion: Any) -> Reply:
    if len(completion.choices) != 1:
        raise ModelError("unexpected_choice_count")
    choice = completion.choices[0]
    if choice.finish_reason not in ("stop", "tool_calls"):
        raise ModelError("completion_not_finished")
    message = choice.message
    if getattr(message, "refusal", None):
        raise ModelError("model_refusal")
    calls: list[ToolCall] = []
    for call in message.tool_calls or []:
        if call.type != "function":
            raise ModelError("unsupported_tool_call")
        calls.append(
            ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments)
        )
    # Non-thinking mode has no reasoning continuation. Never store reasoning_content as text.
    return Reply(
        text=message.content or "",
        calls=calls,
        input_tokens=completion.usage.prompt_tokens if completion.usage else None,
        output_tokens=completion.usage.completion_tokens if completion.usage else None,
    )


class DashScopeModel:
    def __init__(
        self,
        model: str,
        *,
        api_key: str,
        base_url: str,
        use_env_proxy: bool = True,
    ) -> None:
        from openai import DefaultHttpxClient, OpenAI

        self.model = model
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
            http_client=DefaultHttpxClient(trust_env=use_env_proxy),
        )

    def close(self) -> None:
        self.client.close()

    def respond(self, context: Context, *, timeout: float, max_output_tokens: int) -> Reply:
        from openai import APIError

        parameters: dict[str, Any] = {
            "model": self.model,
            "messages": chat_messages(context),
            "stream": False,
            "max_tokens": max_output_tokens,
            "timeout": timeout,
            "extra_body": {"enable_thinking": False},
        }
        if context.tools:
            parameters["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in context.tools
            ]
            parameters["parallel_tool_calls"] = False
        try:
            completion = self.client.chat.completions.create(**parameters)
        except APIError as exc:
            # The server body may contain credentials or workspace details.
            raise ModelError(type(exc).__name__) from exc
        return parse_completion(completion)
