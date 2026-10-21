"""自然语言入口：同一套工具支持不同问题，先发现文件，再由模型选择调用。"""

import json
from pathlib import Path

import pytest
from protocol_support import completion, local_api

from learn_agent.application import run_task
from learn_agent.policy import Policy
from learn_agent.tools import LIST_FILES, ToolCall, ToolRegistry


def configure_local_model(monkeypatch, client):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "local-protocol-test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", str(client.base_url))
    monkeypatch.setenv("DASHSCOPE_MODEL", "local")
    monkeypatch.setenv("DASHSCOPE_USE_ENV_PROXY", "false")


@pytest.mark.parametrize("create_summary", [False, True])
def test_fixed_registry_and_model_chosen_files_without_user_supplied_paths(
    tmp_path, monkeypatch, capsys, create_summary
):
    from learn_agent import __main__ as entry

    (tmp_path / "brief.txt").write_text("会议讨论了产品发布。", encoding="utf-8")
    monkeypatch.setattr("learn_agent.__main__.WORKSPACE", tmp_path)
    question = "把资料总结一下并保存成新文件" if create_summary else "现有资料讲了什么"
    responses = [
        completion(name="list_files", arguments={}, finish="tool_calls"),
        completion(path="brief.txt", finish="tool_calls"),
    ]
    if create_summary:
        responses.append(
            completion(
                name="write_file",
                arguments={"path": "my-summary.md", "content": "会议主题：产品发布。"},
                finish="tool_calls",
            )
        )
    responses.append(completion(text="任务结束"))
    with local_api(responses) as (client, requests):
        configure_local_model(monkeypatch, client)
        assert entry.main([question]) == 0
    expected = ["list_files", "read_file", "search_text", "write_file"]
    for request in requests:
        assert [tool["function"]["name"] for tool in request["tools"]] == expected
    assert requests[0]["messages"][-1]["content"] == question
    discovered = json.loads(requests[1]["messages"][-1]["content"])["data"]["entries"]
    assert discovered == [{"path": "brief.txt", "kind": "file"}]
    assert (
        json.loads(requests[2]["messages"][-1]["content"])["data"]["text"] == "会议讨论了产品发布。"
    )
    if create_summary:
        assert (tmp_path / "my-summary.md").read_text() == "会议主题：产品发布。"
    else:
        assert not (tmp_path / "my-summary.md").exists()
    output = capsys.readouterr().out
    assert "任务结束" in output
    assert "产物验收" not in output


def test_interactive_entry_accepts_a_question_without_any_path(tmp_path, monkeypatch):
    from learn_agent import __main__ as entry

    monkeypatch.setattr("learn_agent.__main__.WORKSPACE", tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt: "先看看有哪些资料")
    with local_api([completion(text="你好")]) as (client, requests):
        configure_local_model(monkeypatch, client)
        assert entry.main([]) == 0
    assert requests[0]["messages"][-1]["content"] == "先看看有哪些资料"


@pytest.mark.parametrize("question", ["", "  "])
def test_empty_question_stops_before_creating_model_client(question, capsys):
    from learn_agent import __main__ as entry

    with pytest.raises(SystemExit) as exc:
        entry.main([question])
    assert exc.value.code == 2
    assert "任务不能为空" in capsys.readouterr().err


@pytest.mark.parametrize("question", ["看看资料", "创建一份报告"])
def test_enabled_tools_come_from_config_even_when_question_requests_more(
    tmp_path, monkeypatch, question
):
    with local_api([completion(text="本次没有执行工具")]) as (client, requests):
        configure_local_model(monkeypatch, client)
        run = run_task(question, tmp_path, Policy(), definitions=(LIST_FILES,))
    assert [tool["function"]["name"] for tool in requests[0]["tools"]] == ["list_files"]
    assert run.answer == "本次没有执行工具"
    assert run.tool_results == ()


def list_directory(workspace, path=".", *, allow_read=True):
    return ToolRegistry([LIST_FILES], workspace, Policy(allow_read=allow_read)).execute(
        ToolCall("listing", "list_files", json.dumps({"path": path}))
    )


def test_directory_discovery_excludes_hidden_files_links_and_allows_subdirectories(tmp_path):
    (tmp_path / "notes.txt").write_text("资料")
    (tmp_path / ".env").write_text("private")
    (tmp_path / "alias.txt").symlink_to("notes.txt")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/inner.txt").write_text("内部资料")
    root = list_directory(tmp_path)
    assert root.content["ok"]
    assert root.call_id == "listing"
    assert {item["path"] for item in root.content["data"]["entries"]} == {"notes.txt", "sub"}
    assert root.content["data"]["truncated"] is False
    nested = list_directory(tmp_path, "sub")
    assert nested.content["data"]["entries"] == [{"path": "sub/inner.txt", "kind": "file"}]


@pytest.mark.parametrize("count", [100, 101])
def test_directory_result_is_bounded_and_marks_real_truncation(tmp_path, count):
    for number in range(count):
        (tmp_path / f"{number}.txt").touch()
    data = list_directory(tmp_path).content["data"]
    assert len(data["entries"]) == 100
    assert data["truncated"] is (count > 100)


@pytest.mark.parametrize("path", ["../", "/tmp", ".hidden", "nul\x00", "escape"])
def test_directory_cannot_escape_workspace_or_expose_hidden_paths(tmp_path, path):
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    result = list_directory(tmp_path, path)
    assert result.content["error"]["code"] == "resource_denied"


def test_directory_listing_obeys_read_policy(tmp_path):
    result = list_directory(tmp_path, allow_read=False)
    assert result.content["error"]["code"] == "policy_denied"


def test_workspace_creation_permission_does_not_allow_other_operations_or_paths():
    policy = Policy(allow_create=True)
    from learn_agent.policy import ActionRequest

    assert policy.evaluate(ActionRequest("create", Path("model-chosen.md"))).allowed
    for operation, path in [
        ("create", "../outside.md"),
        ("create", ".env"),
        ("overwrite", "model-chosen.md"),
        ("delete", "notes.txt"),
    ]:
        assert not policy.evaluate(ActionRequest(operation, Path(path))).allowed
