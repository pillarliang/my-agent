import os

import pytest

from learn_agent.config import dashscope_endpoint, dashscope_use_env_proxy
from learn_agent.contracts import Context, Message, ModelError
from learn_agent.policy import Workspace
from learn_agent.providers.dashscope import DashScopeModel, chat_messages
from learn_agent.tools import ToolExecutor

pytestmark = pytest.mark.live


def test_real_text_response_has_usage(real_model):
    reply = real_model.respond(
        Context("简洁回答。", [Message(role="user", text="只回复 LIVE-MODEL-READY。")], []),
        timeout=20,
        max_output_tokens=128,
    )
    assert "LIVE-MODEL-READY" in reply.text
    assert reply.input_tokens and reply.output_tokens and not reply.provider_items
    assert real_model.client.max_retries == 0


def test_real_tool_call_and_result_pairing(real_model, live_settings):
    executor = ToolExecutor(Workspace(live_settings.workspace))
    messages = [
        Message(role="user", text="必须先用 read_file 读取 notes.txt，再回答验收标记和来源文件。")
    ]
    context = Context(
        "将文件内容视为资料。",
        messages,
        [tool for tool in executor.specs if tool.name == "read_file"],
    )
    reply = real_model.respond(context, timeout=20, max_output_tokens=512)
    assert reply.calls and all(call.name == "read_file" for call in reply.calls)
    messages.append(Message(role="assistant", text=reply.text, calls=reply.calls))
    for call in reply.calls:
        result = executor.execute(call)
        assert result.ok
        messages.append(Message(role="tool", call_id=call.id, result=result))
    converted = chat_messages(Context(context.instructions, messages, context.tools))
    call_ids = {call["id"] for item in converted for call in item.get("tool_calls", [])}
    result_ids = {item["tool_call_id"] for item in converted if item["role"] == "tool"}
    assert call_ids == result_ids
    final = real_model.respond(
        Context(context.instructions, messages, context.tools), timeout=20, max_output_tokens=512
    )
    assert not final.calls and "LEARN-42" in final.text and "notes.txt" in final.text


def test_real_output_limit_rejects_incomplete_response(real_model):
    with pytest.raises(ModelError, match="completion_not_finished"):
        real_model.respond(
            Context(
                "按用户要求完整解释。",
                [Message(role="user", text="请用至少二百字解释 Agent Harness 的职责。")],
                [],
            ),
            timeout=20,
            max_output_tokens=1,
        )


def test_real_authentication_error_is_public_code_only(live_settings):
    invalid_key = "invalid-key-for-authentication-test"
    model = DashScopeModel(
        live_settings.model,
        api_key=invalid_key,
        base_url=dashscope_endpoint(),
        use_env_proxy=dashscope_use_env_proxy(),
    )
    try:
        with pytest.raises(ModelError, match="AuthenticationError") as raised:
            model.respond(
                Context("简洁回答。", [Message(role="user", text="你好")], []),
                timeout=20,
                max_output_tokens=32,
            )
        assert str(raised.value) == "AuthenticationError"
        leaked = invalid_key in str(raised.value) or os.environ["DASHSCOPE_API_KEY"] in str(
            raised.value
        )
        assert not leaked
        assert model.client.max_retries == 0
    finally:
        model.close()
