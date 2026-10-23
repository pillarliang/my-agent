"""会话 MVP：真实文件、重开历史、失败片段和本机 SDK 协议，不请求外部模型。"""

import json

import pytest
from protocol_support import completion, local_api

from learn_agent.application import run_task
from learn_agent.models import (
    ModelError,
    ModelResponse,
    OpenAIChatModel,
    ProviderContinuation,
    TextMessage,
    TokenUsage,
)
from learn_agent.policy import Policy
from learn_agent.runner import run_agent
from learn_agent.session import SessionLog, list_sessions, read_session
from learn_agent.tools import READ_FILE, ToolCall, ToolRegistry, ToolResult


def test_read_then_follow_up_after_reopening(tmp_path):
    directory = tmp_path / "sessions"
    (tmp_path / "notes.txt").write_text("验收标记：LEARN-42", encoding="utf-8")
    tools = ToolRegistry([READ_FILE], tmp_path)
    with local_api(
        [
            completion(path="notes.txt", finish="tool_calls"),
            completion(text="标记是 LEARN-42"),
            completion(text="刚才的标记是 LEARN-42"),
        ]
    ) as (client, requests):
        with SessionLog(directory, workspace=tmp_path) as session:
            result = run_agent(
                OpenAIChatModel(client, "local"), "读取 notes.txt", tools, session=session
            )
            session_id = session.header.session_id
            assert result.session_id == session_id
            assert result.turn_id is not None
        # 真正关闭文件再重开；保持端点不变，历史带入请求，不重复执行旧工具。
        with SessionLog(directory, session_id=session_id) as session:
            result = run_agent(
                OpenAIChatModel(client, "local"), "刚才的标记是什么？", tools, session=session
            )
            assert result.tool_results == ()
    assert len(requests) == 3
    messages = requests[2]["messages"]
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert "LEARN-42" in messages[3]["content"]
    assert messages[-1]["content"] == "刚才的标记是什么？"
    data = read_session(directory, session_id)
    assert [record["seq"] for record in data["records"]] == list(range(1, 11))
    assert [
        record["payload"]["status"]
        for record in data["records"]
        if record["type"] == "turn_finished"
    ] == ["completed", "completed"]
    assert len({record["turn_id"] for record in data["records"]}) == 2
    assert list_sessions(directory)[0]["session_id"] == session_id


def test_message_roundtrip_preserves_response_and_tool_failure(tmp_path):
    response = ModelResponse(
        "先读取",
        (ToolCall("c1", "read_file", '{"path":"missing.txt"}'),),
        "tool_calls",
        "local",
        TokenUsage(12, 3, 15),
        continuation=ProviderContinuation("example", {"opaque": ["材料"]}),
    )
    receipt = ToolResult(
        "c1", "read_file", {"ok": False, "error": {"code": "not_found", "message": "无此文件"}}
    )
    final = ModelResponse("读取失败", (), "stop", "local")
    with SessionLog(tmp_path, workspace=tmp_path) as session:
        turn_id = session.start_turn("读取")
        for message in (response, receipt, final):
            session.append_message(turn_id, message)
        session.finish_turn(turn_id)
        session_id = session.header.session_id
    with SessionLog(tmp_path, session_id=session_id) as reopened:
        assert reopened.history() == (TextMessage("user", "读取"), response, receipt, final)


def test_failed_and_interrupted_turns_are_queryable_but_excluded_from_history(tmp_path):
    tools = ToolRegistry([READ_FILE], tmp_path)
    with local_api([completion(path="notes.txt", finish="length")]) as (client, requests):
        with SessionLog(tmp_path, workspace=tmp_path) as session:
            session_id = session.header.session_id
            with pytest.raises(ModelError, match="未正常完成"):
                run_agent(OpenAIChatModel(client, "local"), "会截断的任务", tools, session=session)
            interrupted = session.start_turn("未结束的问题")
            session.append_message(
                interrupted,
                ModelResponse(None, (ToolCall("x", "read_file", "{}"),), "tool_calls", "local"),
            )
    with local_api([completion(text="新任务结束")]) as (client, new_requests):
        with SessionLog(tmp_path, session_id=session_id) as session:
            assert session.history() == ()
            run_agent(OpenAIChatModel(client, "local"), "新任务", tools, session=session)
    assert len(requests) == len(new_requests) == 1
    assert len(new_requests[0]["messages"]) == 2
    records = read_session(tmp_path, session_id)["records"]
    assert records[2]["payload"]["value"]["stop_reason"] == "length"
    assert records[3]["payload"]["error"]["code"] == "length"
    assert any(record["turn_id"] == interrupted for record in records)


def test_workspace_mismatch_checked_before_creating_model(tmp_path, monkeypatch):
    def unexpected_model():
        pytest.fail("工作区错误时不应创建模型")

    monkeypatch.setattr("learn_agent.application.LLMService", unexpected_model)
    with SessionLog(tmp_path, workspace=tmp_path) as session:
        with pytest.raises(ValueError, match="工作目录"):
            run_task("读取", tmp_path / "elsewhere", Policy(), session=session)


def test_locks_prevent_second_writer_and_partial_detail_reads(tmp_path):
    with SessionLog(tmp_path, workspace=tmp_path) as session:
        session_id = session.header.session_id
        with pytest.raises(ValueError, match="另一个执行者"):
            SessionLog(tmp_path, session_id=session_id)
        with pytest.raises(ValueError, match="正在写入"):
            read_session(tmp_path, session_id)
        assert list_sessions(tmp_path)[0]["session_id"] == session_id
    assert read_session(tmp_path, session_id)["records"] == []


@pytest.mark.parametrize("damage", ["version", "partial", "sequence", "tool_pair", "system"])
def test_corrupt_or_inconsistent_history_fails_explicitly(tmp_path, damage):
    with SessionLog(tmp_path, workspace=tmp_path) as session:
        turn = session.start_turn("读取")
        session.append_message(
            turn, ModelResponse(None, (ToolCall("c", "read_file", "{}"),), "tool_calls", "local")
        )
        session.append_message(turn, ToolResult("c", "read_file", {"ok": True, "data": {}}))
        session.append_message(turn, ModelResponse("结束", (), "stop", "local"))
        session.finish_turn(turn)
        path, session_id = session.path, session.header.session_id
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    if damage == "version":
        lines[0]["version"] = 2
    elif damage == "sequence":
        lines[2]["seq"] = 99
    elif damage == "tool_pair":
        lines[4]["payload"]["value"]["call_id"] = "wrong"
    elif damage == "system":
        lines[2]["payload"]["value"]["role"] = "system"
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + ("" if damage == "partial" else "\n")
    )
    with pytest.raises(ValueError):
        SessionLog(tmp_path, session_id=session_id)
    with pytest.raises(ValueError):
        read_session(tmp_path, session_id)


def test_invalid_id_does_not_open_arbitrary_path(tmp_path):
    with pytest.raises(ValueError, match="会话 ID"):
        SessionLog(tmp_path, session_id="../outside")


def test_cli_queries_need_no_model_and_follow_up_uses_saved_history(tmp_path, monkeypatch, capsys):
    from test_application import configure_local_model

    from learn_agent import __main__ as entry

    monkeypatch.setattr(entry, "WORKSPACE", tmp_path)
    with local_api([completion(text="第一问结束"), completion(text="第二问结束")]) as (
        client,
        requests,
    ):
        configure_local_model(monkeypatch, client)
        entry.main(["第一问"])
        session_id = list_sessions(entry.SESSIONS_DIRECTORY)[0]["session_id"]
        entry.main(["--session", session_id, "第二问"])
    assert len(requests[1]["messages"]) == 4
    capsys.readouterr()
    monkeypatch.setattr(
        "learn_agent.application.LLMService", lambda: pytest.fail("查询不能创建模型")
    )
    assert entry.main(["--list-sessions"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["session_id"] == session_id
    assert entry.main(["--show-session", session_id]) == 0
    assert len(json.loads(capsys.readouterr().out)["records"]) == 8


def test_write_failure_stops_tools_and_does_not_append_completion(tmp_path, monkeypatch):
    tools = ToolRegistry([READ_FILE], tmp_path)
    with SessionLog(tmp_path, workspace=tmp_path) as session:
        original = session._append

        def fail_response(record):
            if record.type == "message" and record.payload["kind"] == "assistant":
                session.write_failed = True
                raise OSError("磁盘已满")
            original(record)

        monkeypatch.setattr(session, "_append", fail_response)
        monkeypatch.setattr(tools, "execute", lambda call: pytest.fail("记录失败后不执行工具"))
        with local_api([completion(path="notes.txt", finish="tool_calls")]) as (client, _):
            with pytest.raises(OSError, match="磁盘已满"):
                run_agent(OpenAIChatModel(client, "local"), "读取", tools, session=session)
        session_id = session.header.session_id
    assert [r["type"] for r in read_session(tmp_path, session_id)["records"]] == [
        "turn_started",
        "message",
    ]


def test_cli_history_survives_two_processes(tmp_path):
    import os
    import subprocess
    import sys

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("验收标记：LEARN-42", encoding="utf-8")
    directory = tmp_path / "history"
    # 两个独立解释器执行同一入口；仅把工作目录与记录目录改到临时位置。
    program = (
        "import sys; from pathlib import Path; from learn_agent import __main__ as entry; "
        "entry.WORKSPACE=Path(sys.argv[1]); entry.SESSIONS_DIRECTORY=Path(sys.argv[2]); "
        "raise SystemExit(entry.main(sys.argv[3:]))"
    )
    with local_api(
        [
            completion(path="notes.txt", finish="tool_calls"),
            completion(text="标记是 LEARN-42"),
            completion(text="刚才的标记是 LEARN-42"),
        ]
    ) as (client, requests):
        env = dict(
            os.environ,
            DASHSCOPE_API_KEY="local",
            DASHSCOPE_BASE_URL=str(client.base_url),
            DASHSCOPE_MODEL="local",
            DASHSCOPE_USE_ENV_PROXY="false",
        )
        command = [sys.executable, "-c", program, str(workspace), str(directory)]
        first = subprocess.run(
            [*command, "读取 notes.txt"], env=env, capture_output=True, text=True, timeout=15
        )
        assert first.returncode == 0, first.stderr
        session_id = list_sessions(directory)[0]["session_id"]
        second = subprocess.run(
            [*command, "--session", session_id, "刚才的标记是什么？"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert second.returncode == 0, second.stderr
    assert "LEARN-42" in second.stdout
    assert len(requests) == 3
    assert [m["role"] for m in requests[2]["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
