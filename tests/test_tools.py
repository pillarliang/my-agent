"""工具契约检查：真实文件、第三个工具、失败语义与模型消息续接。"""

import json

import pytest
from protocol_support import completion, local_api
from pydantic import Field, JsonValue

from learn_agent.models import OpenAIChatModel
from learn_agent.runner import run_agent
from learn_agent.tools import (
    READ_FILE,
    SEARCH_TEXT,
    ToolArguments,
    ToolCall,
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolRegistry,
)


@pytest.fixture
def tools(tmp_path):
    return ToolRegistry([READ_FILE, SEARCH_TEXT], ToolContext(tmp_path))


def invoke(tools, name, arguments):
    return tools.execute(ToolCall("call_original", name, json.dumps(arguments)))


def test_registration_rejects_duplicates_and_invalid_names(tmp_path):
    with pytest.raises(ValueError, match="重复注册"):
        ToolRegistry([READ_FILE, READ_FILE], ToolContext(tmp_path))
    with pytest.raises(ValueError, match="非法工具名"):
        ToolRegistry(
            [ToolDefinition("bad name", "invalid", READ_FILE.arguments_type, READ_FILE.handler)],
            ToolContext(tmp_path),
        )


def test_specs_come_from_registered_argument_models(tools):
    assert [spec["name"] for spec in tools.specs] == ["read_file", "search_text"]
    schema = tools.specs[1]["parameters"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"path", "query"}
    assert schema["properties"]["max_matches"]["maximum"] == 50
    # 修改展示用的声明不能改变注册项或后续声明。
    schema["properties"].clear()
    assert "query" in tools.specs[1]["parameters"]["properties"]


def test_read_result_keeps_identity_and_real_file_content(tools, tmp_path):
    (tmp_path / "notes.txt").write_text("验收标记：LEARN-42", encoding="utf-8")
    result = invoke(tools, "read_file", {"path": "notes.txt"})
    assert (result.call_id, result.name) == ("call_original", "read_file")
    assert result.content == {
        "ok": True,
        "data": {"path": "notes.txt", "text": "验收标记：LEARN-42"},
    }


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        "[]",
        "null",
        '{"path": 1}',
        '{"path": ""}',
        '{"path": "x", "extra": true}',
    ],
)
def test_bad_arguments_do_not_reach_handler(tmp_path, raw):
    def must_not_run(arguments, context):
        pytest.fail("非法参数不应触发执行函数")

    registry = ToolRegistry(
        [ToolDefinition("read_file", "read", READ_FILE.arguments_type, must_not_run)],
        ToolContext(tmp_path),
    )
    result = registry.execute(ToolCall("bad_args", "read_file", raw))
    assert result.call_id == "bad_args"
    assert result.content["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize("limit", [0, 51, True, "2"])
def test_search_limit_uses_strict_shared_schema(tools, limit):
    result = invoke(
        tools, "search_text", {"path": "notes.txt", "query": "目标", "max_matches": limit}
    )
    assert result.content["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("../outside.txt", "resource_denied"),
        ("/etc/passwd", "resource_denied"),
        (".env", "resource_denied"),
        ("nul\x00.txt", "resource_denied"),
        ("missing.txt", "file_error"),
        (".", "file_error"),
        ("large.txt", "file_too_large"),
        ("binary.txt", "invalid_encoding"),
    ],
)
def test_both_tools_share_resource_checks(tools, tmp_path, path, code):
    (tmp_path / "large.txt").write_bytes(b"x" * 16_001)
    (tmp_path / "binary.txt").write_bytes(b"\xff")
    for name, arguments in (
        ("read_file", {"path": path}),
        ("search_text", {"path": path, "query": "x"}),
    ):
        result = invoke(tools, name, arguments)
        assert result.content["error"]["code"] == code


def test_symlink_cannot_escape_or_expose_hidden_target(tools, tmp_path):
    (tmp_path / "outside").symlink_to(tmp_path.parent, target_is_directory=True)
    (tmp_path / ".env").write_text("private", encoding="utf-8")
    (tmp_path / "alias.txt").symlink_to(tmp_path / ".env")
    for path in ("outside/file.txt", "alias.txt"):
        assert invoke(tools, "read_file", {"path": path}).content["error"]["code"] == (
            "resource_denied"
        )


def test_symlink_loop_is_an_expected_file_failure(tools, tmp_path):
    (tmp_path / "loop").symlink_to("loop")
    result = invoke(tools, "read_file", {"path": "loop"})
    assert result.content["error"]["code"] == "file_error"


def test_search_line_numbers_literal_matching_and_exact_truncation(tools, tmp_path):
    (tmp_path / "notes.txt").write_text("目标一\n无关\n目标二\n目标三\nA.*B\n", encoding="utf-8")
    limited = invoke(tools, "search_text", {"path": "notes.txt", "query": "目标", "max_matches": 2})
    assert limited.content["data"] == {
        "path": "notes.txt",
        "query": "目标",
        "matches": [{"line": 1, "text": "目标一"}, {"line": 3, "text": "目标二"}],
        "truncated": True,
    }
    exact = invoke(tools, "search_text", {"path": "notes.txt", "query": "目标", "max_matches": 3})
    assert exact.content["data"]["truncated"] is False
    for query in ("不存在", "a.*b"):
        assert (
            invoke(tools, "search_text", {"path": "notes.txt", "query": query}).content["data"][
                "matches"
            ]
            == []
        )
    assert invoke(tools, "search_text", {"path": "notes.txt", "query": ".*"}).content["data"][
        "matches"
    ] == [{"line": 5, "text": "A.*B"}]


def test_expected_failure_is_feedback_but_programming_error_escapes(tmp_path):
    def expected(arguments, context):
        raise ToolExecutionError("not_available", "资料暂不可用")

    def unexpected(arguments, context):
        raise TypeError("程序错误应停止运行")

    registry = ToolRegistry(
        [
            ToolDefinition("expected", "expected", ToolArguments, expected),
            ToolDefinition("unexpected", "unexpected", ToolArguments, unexpected),
        ],
        ToolContext(tmp_path),
    )
    assert invoke(registry, "unknown", {}).content["error"]["code"] == "unknown_tool"
    assert invoke(registry, "expected", {}).content["error"] == {
        "code": "not_available",
        "message": "资料暂不可用",
    }
    with pytest.raises(TypeError, match="程序错误"):
        invoke(registry, "unexpected", {})


def test_third_tool_integrates_without_loop_changes(tmp_path):
    class AddArguments(ToolArguments):
        left: int = Field(description="First integer.")
        right: int = Field(description="Second integer.")

    def add(arguments: AddArguments, context: ToolContext) -> dict[str, JsonValue]:
        return {"sum": arguments.left + arguments.right}

    registry = ToolRegistry(
        [ToolDefinition("add", "Add two integers.", AddArguments, add)], ToolContext(tmp_path)
    )
    with local_api(
        [
            completion(name="add", arguments={"left": 2, "right": 3}, finish="tool_calls"),
            completion(text="5"),
        ]
    ) as (client, requests):
        assert run_agent(OpenAIChatModel(client, "local"), "计算 2+3", registry) == "5"
    assert requests[0]["tools"][0]["function"]["name"] == "add"
    feedback = requests[1]["messages"][-1]
    assert feedback["tool_call_id"] == "call_0"
    assert json.loads(feedback["content"]) == {"ok": True, "data": {"sum": 5}}


def test_search_and_read_results_reach_next_sdk_request(tools, tmp_path):
    (tmp_path / "notes.txt").write_text("验收标记：LEARN-42", encoding="utf-8")
    with local_api(
        [
            completion(
                name="search_text",
                arguments={"path": "notes.txt", "query": "验收标记"},
                finish="tool_calls",
            ),
            completion(path="notes.txt", finish="tool_calls"),
            completion(text="notes.txt:1 LEARN-42"),
        ]
    ) as (client, requests):
        assert (
            run_agent(OpenAIChatModel(client, "local"), "找到验收标记", tools)
            == "notes.txt:1 LEARN-42"
        )
    messages = requests[2]["messages"]
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
    ]
    search = json.loads(messages[3]["content"])["data"]
    assert search["matches"] == [{"line": 1, "text": "验收标记：LEARN-42"}]
    assert json.loads(messages[5]["content"])["data"]["text"] == "验收标记：LEARN-42"


def test_failed_tool_result_enters_next_sdk_request(tools):
    with local_api(
        [completion(path="missing.txt", finish="tool_calls"), completion(text="文件不存在")]
    ) as (client, requests):
        assert run_agent(OpenAIChatModel(client, "local"), "读取文件", tools) == "文件不存在"
    assert json.loads(requests[1]["messages"][-1]["content"])["error"]["code"] == "file_error"


def test_new_loop_preserves_call_batch_and_request_limit(tools, tmp_path):
    (tmp_path / "notes.txt").write_text("资料", encoding="utf-8")
    with local_api([completion(path="notes.txt", finish="tool_calls", count=2)]) as (
        client,
        requests,
    ):
        with pytest.raises(RuntimeError, match="2 次模型请求上限"):
            run_agent(OpenAIChatModel(client, "local"), "读取", tools, max_steps=2)
    assert [item["tool_call_id"] for item in requests[1]["messages"][-2:]] == ["call_0", "call_1"]
    assert len(requests) == 2


def test_stage_one_main_wires_registered_tools_and_configuration(tmp_path, monkeypatch, capsys):
    from learn_agent import __main__ as entry

    (tmp_path / "notes.txt").write_text("验收标记：LEARN-42", encoding="utf-8")
    monkeypatch.setattr(entry, "WORKSPACE", tmp_path)
    with local_api([completion(text="入口已运行")]) as (client, requests):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "local-protocol-test")
        monkeypatch.setenv("DASHSCOPE_BASE_URL", str(client.base_url))
        monkeypatch.setenv("DASHSCOPE_MODEL", "local")
        monkeypatch.setenv("DASHSCOPE_USE_ENV_PROXY", "false")
        entry.main([])
    assert [tool["function"]["name"] for tool in requests[0]["tools"]] == [
        "read_file",
        "search_text",
    ]
    assert "入口已运行" in capsys.readouterr().out
