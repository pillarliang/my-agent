"""工具契约检查：真实文件、函数注册、统一授权与模型消息续接。"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from protocol_support import completion, local_api

from examples.count_lines import count_lines
from learn_agent.models import OpenAIChatModel
from learn_agent.policy import ActionRequest, Policy, PolicyDecision
from learn_agent.runner import run_agent
from learn_agent.tools import (
    LIST_FILES,
    READ_FILE,
    SEARCH_TEXT,
    ToolArguments,
    ToolCall,
    ToolDefinition,
    ToolExecutionError,
    ToolRegistry,
    filesystem,
)


@pytest.fixture
def tools(tmp_path):
    return ToolRegistry([READ_FILE, SEARCH_TEXT], tmp_path)


def invoke(tools, name, arguments):
    return tools.execute(ToolCall("call_original", name, json.dumps(arguments)))


def test_registration_rejects_duplicates_and_invalid_names(tmp_path):
    with pytest.raises(ValueError, match="重复注册"):
        ToolRegistry([READ_FILE, READ_FILE], tmp_path)

    with pytest.raises(ValueError, match="非法工具名"):
        ToolRegistry([replace(READ_FILE, name="bad name")], tmp_path)


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
def test_bad_arguments_do_not_reach_tool(tmp_path, raw):
    def must_not_run(arguments, workspace, action):
        pytest.fail("非法参数不应触发文件读取")

    definition = replace(READ_FILE, function=must_not_run)
    registry = ToolRegistry([definition], tmp_path)
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
    (tmp_path / "notes.txt").write_text("资料", encoding="utf-8")
    registry = ToolRegistry([], tmp_path)
    assert invoke(registry, "unknown", {}).content["error"]["code"] == "unknown_tool"

    def expected(arguments, workspace, action):
        raise ToolExecutionError("not_available", "资料暂不可用")

    def unexpected(arguments, workspace, action):
        raise TypeError("程序错误应停止运行")

    registry.register(
        expected, filesystem.ReadFileArguments, permission="read", path_parameter="path"
    )
    registry.register(
        unexpected, filesystem.ReadFileArguments, permission="read", path_parameter="path"
    )
    assert invoke(registry, "expected", {"path": "notes.txt"}).content["error"] == {
        "code": "not_available",
        "message": "资料暂不可用",
    }
    with pytest.raises(TypeError, match="程序错误"):
        invoke(registry, "unexpected", {"path": "notes.txt"})


def test_registration_requires_permission(tmp_path):
    def custom(arguments, workspace, action):
        return {}

    registry = ToolRegistry([], tmp_path)
    with pytest.raises(TypeError, match="permission"):
        registry.register(custom, ToolArguments)
    with pytest.raises(ValueError, match="不支持的工具权限"):
        registry.register(custom, ToolArguments, permission=None)
    with pytest.raises(TypeError, match="function|permission"):
        ToolDefinition("missing", "Incomplete registration.", ToolArguments)


@pytest.mark.parametrize("permission", ["read", "write", "list"])
@pytest.mark.parametrize("path_parameter", [None, "", "missing", "count"])
def test_file_permission_requires_existing_string_parameter(tmp_path, permission, path_parameter):
    class Arguments(ToolArguments):
        path: str
        count: int

    def custom(arguments, workspace, action):
        pytest.fail("无效权限配置不应执行")

    registry = ToolRegistry([], tmp_path)
    with pytest.raises(ValueError, match="字符串路径字段"):
        registry.register(custom, Arguments, permission=permission, path_parameter=path_parameter)
    assert registry.specs == []


def test_compute_permission_rejects_unused_path_parameter(tmp_path):
    with pytest.raises(ValueError, match="不使用路径参数"):
        ToolRegistry([], tmp_path).register(
            count_lines, filesystem.ReadFileArguments, permission="compute", path_parameter="path"
        )


@pytest.mark.parametrize("allow_overwrite", [False, True])
def test_registry_authorizes_new_plain_function_without_manual_check(tmp_path, allow_overwrite):
    target = tmp_path / "notes.txt"
    target.write_text("原内容", encoding="utf-8")
    called = []

    def custom_write(arguments, workspace, action):
        # 新名字、普通函数、没有权限检查：由未修改的注册表控制它能否运行。
        called.append(action)
        (workspace / action.resource).write_text(arguments.content, encoding="utf-8")
        return {"written": True}

    registry = ToolRegistry([], tmp_path, Policy(allow_overwrite=allow_overwrite))
    registry.register(
        custom_write, filesystem.WriteFileArguments, permission="write", path_parameter="path"
    )
    result = invoke(registry, "custom_write", {"path": "notes.txt", "content": "新内容"})
    assert result.call_id == "call_original"
    if allow_overwrite:
        assert result.content == {"ok": True, "data": {"written": True}}
        assert called == [ActionRequest("overwrite", Path("notes.txt"))]
        assert target.read_text(encoding="utf-8") == "新内容"
    else:
        assert result.content["error"]["code"] == "policy_denied"
        assert called == []
        assert target.read_text(encoding="utf-8") == "原内容"


def test_new_function_registers_and_reaches_runner_without_changes(tmp_path):
    (tmp_path / "notes.txt").write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
    actions = []

    class RecordingPolicy:
        def evaluate(self, action):
            actions.append(action)
            return Policy().evaluate(action)

    registry = ToolRegistry([READ_FILE, SEARCH_TEXT], tmp_path, RecordingPolicy())
    registry.register(
        count_lines, filesystem.ReadFileArguments, permission="read", path_parameter="path"
    )
    assert registry.specs[-1]["description"] == count_lines.__doc__
    with local_api(
        [
            completion(name="count_lines", arguments={"path": "notes.txt"}, finish="tool_calls"),
            completion(text="3"),
        ]
    ) as (client, requests):
        assert run_agent(OpenAIChatModel(client, "local"), "统计资料行数", registry).answer == "3"
    assert actions == [ActionRequest("read", Path("notes.txt"))]
    assert [item["function"]["name"] for item in requests[0]["tools"]] == [
        "read_file",
        "search_text",
        "count_lines",
    ]
    feedback = requests[1]["messages"][-1]
    assert feedback["tool_call_id"] == "call_0"
    assert json.loads(feedback["content"]) == {"ok": True, "data": {"lines": 3}}


@pytest.mark.parametrize("allowed", [False, True])
def test_non_file_function_uses_same_policy_gate(tmp_path, allowed):
    class MultiplyArguments(ToolArguments):
        left: int
        right: int

    events = []

    def multiply(arguments, workspace, action):
        events.append("execute")
        return {"product": arguments.left * arguments.right}

    class ComputePolicy:
        def evaluate(self, action):
            events.append("policy")
            assert action == ActionRequest("compute", Path("multiply"))
            return PolicyDecision(allowed, "计算权限")

    registry = ToolRegistry([], tmp_path, ComputePolicy())
    registry.register(multiply, MultiplyArguments, permission="compute")
    result = invoke(registry, "multiply", {"left": 2, "right": 3})
    if allowed:
        assert result.content == {"ok": True, "data": {"product": 6}}
        assert events == ["policy", "execute"]
    else:
        assert result.content["error"]["code"] == "policy_denied"
        assert events == ["policy"]


def test_unknown_permission_cannot_be_registered(tmp_path):
    def remove(arguments, workspace, action):
        pytest.fail("无效声明不应执行")

    registry = ToolRegistry([], tmp_path)
    with pytest.raises(ValueError, match="不支持的工具权限"):
        registry.register(remove, ToolArguments, permission="delete")
    assert registry.specs == []


@pytest.mark.parametrize("allow_read", [False, True])
def test_declared_read_permission_is_still_subject_to_policy(tmp_path, allow_read):
    (tmp_path / "notes.txt").write_text("第一行\n第二行\n", encoding="utf-8")
    registry = ToolRegistry([], tmp_path, Policy(allow_read=allow_read))
    registry.register(
        count_lines, filesystem.ReadFileArguments, permission="read", path_parameter="path"
    )
    result = invoke(registry, "count_lines", {"path": "notes.txt"})
    if allow_read:
        assert result.content == {"ok": True, "data": {"lines": 2}}
    else:
        assert result.content["error"]["code"] == "policy_denied"


def test_declaration_can_reference_differently_named_path_parameter(tmp_path):
    class SourceArguments(ToolArguments):
        source: str

    (tmp_path / "notes.txt").write_text("正文", encoding="utf-8")
    approved = []

    def custom_read(arguments, workspace, action):
        approved.append(action)
        return {"text": filesystem.read_workspace_text(action.resource, workspace)}

    registry = ToolRegistry([], tmp_path)
    registry.register(custom_read, SourceArguments, permission="read", path_parameter="source")
    assert invoke(registry, "custom_read", {"source": "notes.txt"}).content["data"] == {
        "text": "正文"
    }
    assert approved == [ActionRequest("read", Path("notes.txt"))]


@pytest.mark.parametrize("definition", [READ_FILE, LIST_FILES])
def test_changed_resource_is_not_executed_under_previous_approval(tmp_path, definition):
    target = tmp_path / "approved"
    other = tmp_path / "other"
    if definition is READ_FILE:
        target.write_text("已批准", encoding="utf-8")
        other.write_text("未批准", encoding="utf-8")
    else:
        target.mkdir()
        other.mkdir()
        (other / "private.txt").write_text("未批准", encoding="utf-8")

    class RedirectAfterAllow:
        def evaluate(self, action):
            assert action.resource == Path("approved")
            if target.is_dir():
                target.rmdir()
            else:
                target.unlink()
            target.symlink_to(other)
            return PolicyDecision(True, "只批准原路径")

    tools = ToolRegistry([definition], tmp_path, RedirectAfterAllow())
    result = invoke(tools, definition.name, {"path": "approved"})
    assert result.content["error"]["code"] == "resource_changed"


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
            run_agent(OpenAIChatModel(client, "local"), "找到验收标记", tools).answer
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
        assert run_agent(OpenAIChatModel(client, "local"), "读取文件", tools).answer == "文件不存在"
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


def test_main_wires_fixed_tools_and_configuration(tmp_path, monkeypatch, capsys):
    from learn_agent import __main__ as entry

    (tmp_path / "notes.txt").write_text("验收标记：LEARN-42", encoding="utf-8")
    monkeypatch.setattr("learn_agent.__main__.WORKSPACE", tmp_path)
    with local_api([completion(text="入口已运行")]) as (client, requests):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "local-protocol-test")
        monkeypatch.setenv("DASHSCOPE_BASE_URL", str(client.base_url))
        monkeypatch.setenv("DASHSCOPE_MODEL", "local")
        monkeypatch.setenv("DASHSCOPE_USE_ENV_PROXY", "false")
        entry.main(["查看现有资料"])
    assert [tool["function"]["name"] for tool in requests[0]["tools"]] == [
        "list_files",
        "read_file",
        "search_text",
        "write_file",
    ]
    output = capsys.readouterr().out
    assert "入口已运行" in output
    assert "产物验收" not in output
