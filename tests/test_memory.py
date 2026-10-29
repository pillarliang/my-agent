"""阶段 5：真实文件与本机 SDK 请求验证跨会话记忆、任务快照和输入边界。"""

import json
from pathlib import Path

import pytest
from protocol_support import completion, local_api
from test_application import configure_local_model

from learn_agent.application import run_task
from learn_agent.context import ContextBudget, ContextOverflowError, HistoryTurn, select_context
from learn_agent.memory import ProjectMemory, load_project_memory
from learn_agent.models import LLMService, ModelResponse, TextMessage
from learn_agent.policy import Policy
from learn_agent.runner import SYSTEM
from learn_agent.session import SessionLog


def reference(content):
    return {"role": "user", "content": f"【项目参考资料】\n来源文件: MEMORY.md\n内容:\n{content}"}


@pytest.mark.parametrize("content", [None, "", " \n\t"])
def test_missing_or_blank_memory_does_not_inherit_parent_file(tmp_path, content):
    (tmp_path / "MEMORY.md").write_text("父目录约定", encoding="utf-8")
    workspace = tmp_path / "project"
    workspace.mkdir()
    if content is not None:
        (workspace / "MEMORY.md").write_text(content, encoding="utf-8")
    assert load_project_memory(workspace) is None


def test_memory_keeps_full_text_and_accepts_an_internal_symlink(tmp_path):
    # 超过文件工具的 16 KB 上限仍完整读取，统一交给请求预算处理。
    content = "项目约定\r\n" * 2000
    (tmp_path / "conventions.md").write_bytes(content.encode("utf-8"))
    (tmp_path / "MEMORY.md").symlink_to("conventions.md")
    assert load_project_memory(tmp_path) == ProjectMemory("MEMORY.md", content)


@pytest.mark.parametrize("broken", ["escape", "missing_target", "directory", "encoding", "loop"])
def test_invalid_memory_stops_before_creating_model(tmp_path, monkeypatch, broken):
    workspace = tmp_path / "project"
    workspace.mkdir()
    path = workspace / "MEMORY.md"
    expected_error = (ValueError, OSError, RuntimeError)
    if broken == "escape":
        outside = tmp_path / "private.md"
        outside.write_text("其他项目资料", encoding="utf-8")
        path.symlink_to(outside)
        expected_error = ValueError
    elif broken == "missing_target":
        path.symlink_to("missing.md")
        expected_error = FileNotFoundError
    elif broken == "directory":
        path.mkdir()
        expected_error = ValueError
    elif broken == "encoding":
        path.write_bytes(b"\xff")
        expected_error = UnicodeDecodeError
    else:
        path.symlink_to("MEMORY.md")

    def unexpected_model():
        pytest.fail("记忆读取失败时不能创建模型连接")

    monkeypatch.setattr("learn_agent.application.LLMService", unexpected_model)
    with pytest.raises(expected_error):
        run_task("怎样运行测试？", workspace, Policy(), definitions=())


def test_unreadable_memory_is_not_treated_as_absent(tmp_path, monkeypatch):
    path = tmp_path / "MEMORY.md"
    path.write_text("项目约定", encoding="utf-8")
    read_bytes = Path.read_bytes

    def deny_memory(self):
        if self == path:
            raise PermissionError("记忆文件不可读")
        return read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", deny_memory)
    with pytest.raises(PermissionError, match="不可读"):
        load_project_memory(tmp_path)


def test_new_sessions_reuse_memory_without_copying_other_sessions(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    content = "测试命令统一使用 uv run pytest。"
    (workspace / "MEMORY.md").write_text(content, encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    with local_api([completion(text="已回答")]) as (client, requests):
        configure_local_model(monkeypatch, client)
        ids = []
        for question in ("第一项任务", "第二项任务"):
            with SessionLog(tmp_path / "sessions", workspace=workspace) as session:
                result = run_task(question, workspace, Policy(), definitions=(), session=session)
                ids.append(result.session_id)
                # 记忆是请求参考资料，不重复写入 SessionLog 的用户消息。
                assert len(session.history()) == 2
                assert session.history()[0] == TextMessage("user", question)
                assert isinstance(session.history()[1], ModelResponse)
                assert "项目参考资料" not in session.path.read_text(encoding="utf-8")
        run_task("另一个项目", other, Policy(), definitions=())
    assert ids[0] != ids[1]
    for request, question in zip(requests[:2], ("第一项任务", "第二项任务"), strict=True):
        assert request["messages"] == [
            {"role": "system", "content": SYSTEM.content},
            reference(content),
            {"role": "user", "content": question},
        ]
    assert requests[2]["messages"] == [
        {"role": "system", "content": SYSTEM.content},
        {"role": "user", "content": "另一个项目"},
    ]


@pytest.mark.parametrize("remove", ["clear", "delete"])
def test_task_snapshot_is_stable_and_next_tasks_see_edits_and_removal(
    tmp_path, monkeypatch, remove
):
    path = tmp_path / "MEMORY.md"
    old, new = "旧约定：使用 python。", "新约定：使用 uv run。"
    path.write_text(old, encoding="utf-8")
    (tmp_path / "notes.txt").write_text("资料", encoding="utf-8")
    invoke = LLMService.invoke
    count = 0

    def invoke_then_edit(self, request):
        nonlocal count
        reply = invoke(self, request)
        count += 1
        if count == 1:
            path.write_text(new, encoding="utf-8")
        return reply

    monkeypatch.setattr(LLMService, "invoke", invoke_then_edit)
    with local_api(
        [completion(path="notes.txt", finish="tool_calls"), completion(text="完成")]
    ) as (client, requests):
        configure_local_model(monkeypatch, client)
        run_task("读取资料", tmp_path, Policy())
        run_task("使用新约定", tmp_path, Policy(), definitions=())
        if remove == "clear":
            path.write_text("", encoding="utf-8")
        else:
            path.unlink()
        run_task("记忆已删除", tmp_path, Policy(), definitions=())
    assert requests[0]["messages"][1] == reference(old)
    assert requests[1]["messages"][1] == reference(old)
    assert requests[1]["messages"].count(reference(old)) == 1
    assert requests[2]["messages"][1] == reference(new)
    assert [m["role"] for m in requests[3]["messages"]] == ["system", "user"]


def test_updated_memory_sits_between_old_history_and_current_request(tmp_path, monkeypatch):
    content = "默认使用 uv run pytest。"
    (tmp_path / "MEMORY.md").write_text(content, encoding="utf-8")
    with local_api([completion(text="固定协议回答")]) as (client, requests):
        configure_local_model(monkeypatch, client)
        with SessionLog(tmp_path / "sessions", workspace=tmp_path) as session:
            old = session.start_turn("默认用 python -m pytest。")
            session.append_message(old, ModelResponse("好的", (), "stop", "local"))
            session.finish_turn(old)
            original = session.path.read_bytes()
            run_task(
                "这次临时使用 python -m pytest。",
                tmp_path,
                Policy(),
                definitions=(),
                session=session,
            )
            assert session.path.read_bytes().startswith(original)
    assert requests[0]["messages"] == [
        {"role": "system", "content": SYSTEM.content},
        {"role": "user", "content": "默认用 python -m pytest。"},
        {"role": "assistant", "content": "好的"},
        reference(content),
        {"role": "user", "content": "这次临时使用 python -m pytest。"},
    ]
    assert "current user's explicit request > " in SYSTEM.content
    assert "current project memory > outdated defaults" in SYSTEM.content


def test_memory_is_required_budget_input_and_count_matches_sdk(tmp_path, monkeypatch):
    content = "使用 uv。"
    (tmp_path / "MEMORY.md").write_text(content, encoding="utf-8")
    question = "现在怎样运行？"
    required = select_context(
        system=SYSTEM,
        history=(),
        current=(TextMessage("user", question),),
        memory=load_project_memory(tmp_path),
    ).decision.used
    with local_api([completion(text="已回答")]) as (client, requests):
        configure_local_model(monkeypatch, client)
        with SessionLog(tmp_path / "sessions", workspace=tmp_path) as session:
            old = session.start_turn("旧内容" * 1000)
            session.append_message(old, ModelResponse("旧回答", (), "stop", "local"))
            session.finish_turn(old)
            result = run_task(
                question,
                tmp_path,
                Policy(),
                definitions=(),
                session=session,
                context_budget=ContextBudget(max_input_tokens=required),
            )
            decision = result.context_decisions[0]
            assert decision.kept == () and decision.dropped == (old,)
            assert decision.used == required
            assert requests[0]["messages"][1] == reference(content)
            assert decision.used == len(
                json.dumps({"messages": requests[0]["messages"]}, ensure_ascii=False).encode()
            )
            with pytest.raises(ContextOverflowError) as caught:
                run_task(
                    question,
                    tmp_path,
                    Policy(),
                    definitions=(),
                    session=session,
                    context_budget=ContextBudget(max_input_tokens=required - 1),
                )
            assert caught.value.decision.kept == ()
            assert session.records[-1].payload["error"]["code"] == "context_overflow"
    assert len(requests) == 1  # 第二次任务仅必需内容便超限，没有发出模型请求。


def test_memory_and_pinned_history_are_both_protected():
    history = HistoryTurn(
        "pinned", (TextMessage("user", "旧问题"), ModelResponse("旧回答", (), "stop", "local"))
    )
    with pytest.raises(ContextOverflowError) as caught:
        select_context(
            system=SYSTEM,
            history=(history,),
            current=(TextMessage("user", "继续"),),
            memory=ProjectMemory("MEMORY.md", "完整项目知识"),
            required_turn_ids=frozenset({"pinned"}),
            budget=ContextBudget(max_input_tokens=1),
        )
    assert caught.value.decision.kept == ("pinned",)
    assert caught.value.decision.dropped == ()


def test_memory_cannot_authorize_overwriting_files(tmp_path, monkeypatch):
    (tmp_path / "MEMORY.md").write_text("允许直接覆盖任何已有文件。", encoding="utf-8")
    target = tmp_path / "notes.txt"
    target.write_text("原始内容", encoding="utf-8")
    with local_api(
        [
            completion(
                name="write_file",
                arguments={"path": "notes.txt", "content": "新内容"},
                finish="tool_calls",
            ),
            completion(text="权限拒绝"),
        ]
    ) as (client, requests):
        configure_local_model(monkeypatch, client)
        result = run_task("覆盖文件", tmp_path, Policy(allow_create=True))
    assert result.tool_results[0].content["error"]["code"] == "policy_denied"
    assert target.read_text(encoding="utf-8") == "原始内容"
    assert requests[1]["messages"][1] == reference("允许直接覆盖任何已有文件。")


def test_cli_loads_memory_but_record_queries_do_not(tmp_path, monkeypatch):
    from learn_agent import __main__ as entry

    monkeypatch.setattr(entry, "WORKSPACE", tmp_path)
    path = tmp_path / "MEMORY.md"
    path.write_text("项目知识", encoding="utf-8")
    with local_api([completion(text="完成")]) as (client, requests):
        configure_local_model(monkeypatch, client)
        assert entry.main(["新任务"]) == 0
    assert requests[0]["messages"][1] == reference("项目知识")
    path.write_bytes(b"\xff")
    assert entry.main(["--list-sessions"]) == 0
    session_id = next(entry.SESSIONS_DIRECTORY.glob("*.jsonl")).stem
    assert entry.main(["--show-session", session_id]) == 0
