"""模型边界检查：真实 SDK、本机 HTTP 与文件；不请求外部模型。"""

import json
import socket

import pytest
from openai import DefaultHttpxClient, OpenAI
from protocol_support import completion, local_api

from learn_agent.models import (
    ModelError,
    ModelProtocolError,
    ModelRequest,
    ModelResponse,
    OpenAIChatModel,
    TextMessage,
    TokenUsage,
)
from learn_agent.runner import run_agent
from learn_agent.tools import READ_FILE, ToolCall, ToolContext, ToolRegistry, ToolResult


def test_text_request_configuration_and_usage():
    response = completion(text="你好")
    response["usage"] = {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}
    with local_api([response]) as (client, requests):
        model = OpenAIChatModel(client, "physical-model", extra_body={"enable_thinking": False})
        reply = model.invoke(ModelRequest((TextMessage("user", "你好"),), max_output_tokens=64))
    assert reply.text == "你好" and reply.stop_reason == "stop"
    assert reply.model == "local"  # 保存服务实际报告的模型名，不用请求名覆盖它。
    assert reply.usage == TokenUsage(8, 2, 10)
    assert requests == [
        {
            "model": "physical-model",
            "messages": [{"role": "user", "content": "你好"}],
            "max_tokens": 64,
            "enable_thinking": False,
        }
    ]


def test_tools_are_encoded_without_changing_internal_specs(tmp_path):
    tools = ToolRegistry([READ_FILE], ToolContext(tmp_path))
    before = tools.specs
    with local_api([completion(text="完成")]) as (client, requests):
        reply = OpenAIChatModel(client, "local").invoke(
            ModelRequest((TextMessage("system", "只读"),), tuple(tools.specs))
        )
    assert reply.usage is None
    assert requests[0]["tools"] == [{"type": "function", "function": before[0]}]
    assert requests[0]["parallel_tool_calls"] is False
    assert "enable_thinking" not in requests[0]  # 厂商选项由应用明确配置。
    assert tools.specs == before


def test_text_calls_and_provider_continuation_survive_round_trip():
    response = completion(path="notes.txt", text="先读取资料", finish="tool_calls", count=2)
    message = response["choices"][0]["message"]
    message["reasoning_content"] = "供应商返回的续接字段"
    # 故意不是合法 JSON，适配器应保留原文，由工具系统负责参数校验。
    message["tool_calls"][0]["function"]["arguments"] = "not-json"
    with local_api([response, completion(text="完成")]) as (client, requests):
        model = OpenAIChatModel(client, "local")
        first = model.invoke(ModelRequest((TextMessage("user", "读资料"),)))
        assert first.text == "先读取资料"
        assert first.tool_calls == (
            ToolCall("call_0", "read_file", "not-json"),
            ToolCall("call_1", "read_file", '{"path": "notes.txt"}'),
        )
        results = tuple(
            ToolResult(call.call_id, call.name, {"ok": True, "data": {"text": "真实回执"}})
            for call in first.tool_calls
        )
        model.invoke(ModelRequest((TextMessage("user", "读资料"), first, *results)))
    assert requests[1]["messages"][1] == message
    feedback = requests[1]["messages"][2:]
    assert [item["tool_call_id"] for item in feedback] == ["call_0", "call_1"]
    assert json.loads(feedback[0]["content"])["data"]["text"] == "真实回执"


def test_internal_reply_without_continuation_can_be_encoded():
    reply = ModelResponse(
        text="读取文件",
        tool_calls=(ToolCall("internal_call", "read_file", '{"path":"notes.txt"}'),),
        stop_reason="tool_calls",
        model="another-adapter",
    )
    result = ToolResult("internal_call", "read_file", {"ok": True, "data": {"text": "资料"}})
    with local_api([completion(text="完成")]) as (client, requests):
        OpenAIChatModel(client, "local").invoke(ModelRequest((reply, result)))
    assistant = requests[0]["messages"][0]
    assert assistant["content"] == "读取文件"
    assert assistant["tool_calls"][0]["id"] == "internal_call"


def test_response_only_empty_annotations_are_not_sent_as_input():
    response = completion(text="资料")
    response["choices"][0]["message"]["annotations"] = []
    with local_api([response, completion(text="完成")]) as (client, requests):
        model = OpenAIChatModel(client, "local")
        reply = model.invoke(ModelRequest((TextMessage("user", "资料"),)))
        model.invoke(ModelRequest((reply, TextMessage("user", "继续"))))
    assert requests[1]["messages"][0] == {"role": "assistant", "content": "资料"}


@pytest.mark.parametrize("change", ["model", "endpoint"])
def test_native_continuation_rejects_another_model_or_endpoint(change):
    with local_api([completion(text="资料")]) as (first_client, first_requests):
        reply = OpenAIChatModel(first_client, "model-a").invoke(
            ModelRequest((TextMessage("user", "资料"),))
        )
        with local_api([completion(text="不能发送")]) as (second_client, second_requests):
            model = OpenAIChatModel(
                first_client if change == "model" else second_client,
                "model-b" if change == "model" else "model-a",
            )
            with pytest.raises(ModelProtocolError, match="不属于"):
                model.invoke(ModelRequest((reply,)))
    assert len(first_requests) == 1 and second_requests == []


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "request_error"),
        (401, "authentication_error"),
        (403, "authentication_error"),
        (429, "rate_limit"),
        (500, "server_error"),
        (503, "server_error"),
    ],
)
def test_http_errors_keep_status_and_cause_without_retry(status, code):
    response = (
        status,
        {"error": {"message": "服务错误", "type": "test"}},
        {"x-request-id": "req-42"},
    )
    with local_api([response]) as (client, requests):
        # 即使应用传入有自动重试的 SDK client，适配器也保持一次请求一次尝试。
        model = OpenAIChatModel(client.with_options(max_retries=2), "local")
        with pytest.raises(ModelError) as caught:
            model.invoke(ModelRequest((TextMessage("user", "你好"),)))
    assert caught.value.code == code
    assert caught.value.status_code == status
    assert caught.value.request_id == "req-42"
    assert caught.value.__cause__ is not None
    assert len(requests) == 1


def test_timeout_is_a_model_error_and_does_not_retry():
    with local_api([completion(text="太晚")], delay=0.1) as (client, requests):
        model = OpenAIChatModel(client.with_options(timeout=0.02), "local")
        with pytest.raises(ModelError) as caught:
            model.invoke(ModelRequest((TextMessage("user", "你好"),)))
    assert caught.value.code == "timeout"
    assert caught.value.__cause__ is not None
    assert len(requests) == 1


def test_connection_failure_is_a_model_error():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with OpenAI(
        api_key="local-test",
        base_url=f"http://127.0.0.1:{port}/v1",
        timeout=0.1,
        http_client=DefaultHttpxClient(trust_env=False),
    ) as client:
        with pytest.raises(ModelError) as caught:
            OpenAIChatModel(client, "local").invoke(ModelRequest((TextMessage("user", "你好"),)))
    assert caught.value.code == "connection_error"


@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_incomplete_reply_never_executes_tool(tmp_path, reason, capsys):
    response = completion(path="notes.txt", text="未完成", finish=reason)
    (tmp_path / "notes.txt").write_text("资料", encoding="utf-8")
    with local_api([response]) as (client, requests):
        tools = ToolRegistry([READ_FILE], ToolContext(tmp_path))
        with pytest.raises(ModelError, match="未正常完成") as caught:
            run_agent(OpenAIChatModel(client, "local"), "读取", tools)
    assert caught.value.code == reason
    assert len(requests) == 1 and "工具 read_file" not in capsys.readouterr().out


def test_refusal_never_executes_tool(tmp_path, capsys):
    response = completion(path="notes.txt", finish="tool_calls")
    response["choices"][0]["message"]["refusal"] = "拒绝说明"
    with local_api([response]) as (client, requests):
        with pytest.raises(ModelError, match="拒绝") as caught:
            run_agent(
                OpenAIChatModel(client, "local"),
                "读取",
                ToolRegistry([READ_FILE], ToolContext(tmp_path)),
            )
    assert caught.value.code == "refusal"
    assert len(requests) == 1 and "工具 read_file" not in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["empty_choices", "no_calls", "custom_tool", "legacy_function"])
def test_invalid_protocol_stops_at_model_boundary(kind):
    response = completion(path="notes.txt", finish="tool_calls")
    if kind == "empty_choices":
        response["choices"] = []
    elif kind == "no_calls":
        response["choices"][0]["message"]["tool_calls"] = []
    elif kind == "custom_tool":
        response["choices"][0]["message"]["tool_calls"] = [
            {"id": "custom", "type": "custom", "custom": {"name": "shell", "input": "pwd"}}
        ]
    else:
        response["choices"][0]["message"]["function_call"] = {"name": "legacy", "arguments": "{}"}
    with local_api([response]) as (client, requests):
        with pytest.raises(ModelProtocolError):
            OpenAIChatModel(client, "local").invoke(ModelRequest((TextMessage("user", "读取"),)))
    assert len(requests) == 1


def test_empty_text_reply_is_not_reported_as_success(tmp_path):
    with local_api([completion()]) as (client, requests):
        with pytest.raises(ModelError, match="空回复"):
            run_agent(
                OpenAIChatModel(client, "local"),
                "你好",
                ToolRegistry([READ_FILE], ToolContext(tmp_path)),
            )
    assert len(requests) == 1
