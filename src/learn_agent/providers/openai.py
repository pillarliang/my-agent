"""Responses API adapter. Preserve complete output items for continuation."""

from typing import Any, cast

from learn_agent.contracts import Context, ModelError, Reply, ToolCall


def input_items(context: Context) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in context.messages:
        if message.role == "user":
            items.append({"role": "user", "content": message.text})
        elif message.role == "assistant":
            # Includes opaque reasoning/encrypted continuation and function_call items.
            # Do not reconstruct just text + calls: reasoning models need other items.
            items.extend(message.provider_items)
        elif message.result:
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.call_id,
                    "output": message.result.model_dump_json(),
                }
            )
    return items


def parse_response(response: Any) -> Reply:
    if response.status != "completed":
        raise ModelError("response_not_completed")
    calls: list[ToolCall] = []
    text: list[str] = []
    for item in response.output:
        if item.type == "function_call":
            calls.append(ToolCall(id=item.call_id, name=item.name, arguments=item.arguments))
        elif item.type == "message":
            for content in item.content:
                if content.type == "refusal":
                    raise ModelError("model_refusal")
                if content.type == "output_text":
                    text.append(content.text)
        elif item.type != "reasoning":
            raise ModelError("unsupported_output_item")
    return Reply(
        text="\n".join(text),
        calls=calls,
        provider_items=[
            item.model_dump(mode="json", exclude_none=True) for item in response.output
        ],
        input_tokens=response.usage.input_tokens if response.usage else None,
        output_tokens=response.usage.output_tokens if response.usage else None,
    )


class OpenAIModel:
    def __init__(self, model: str) -> None:
        from openai import OpenAI

        self.model = model
        self.client = OpenAI(max_retries=0)

    def close(self) -> None:
        self.client.close()

    def respond(self, context: Context, *, timeout: float, max_output_tokens: int) -> Reply:
        from openai import APIError
        from openai.types.responses import ResponseInputParam

        try:
            response = self.client.responses.create(
                model=self.model,
                instructions=context.instructions,
                input=cast(ResponseInputParam, input_items(context)),
                tools=[
                    {
                        "type": "function",
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                        "strict": True,
                    }
                    for tool in context.tools
                ],
                parallel_tool_calls=False,
                store=False,
                include=["reasoning.encrypted_content"],
                max_output_tokens=max_output_tokens,
                timeout=timeout,
            )
        except APIError as exc:
            raise ModelError(type(exc).__name__) from exc
        return parse_response(response)
