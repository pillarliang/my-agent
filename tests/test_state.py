"""持久记录契约：真实 SQLite、文件和本机 SDK 协议，不调用外部模型。"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from protocol_support import completion, local_api

from learn_agent.application import run_task
from learn_agent.models import ModelError, OpenAIChatModel
from learn_agent.policy import Policy
from learn_agent.runner import SYSTEM, run_agent
from learn_agent.state import SessionStore
from learn_agent.tools import READ_FILE, WRITE_FILE, ToolRegistry


def test_restart_restores_messages_receipts_usage_and_native_continuation(tmp_path):
    db = tmp_path / "records.sqlite3"
    (tmp_path / "notes.txt").write_text("真实资料", encoding="utf-8")
    first = completion(path="notes.txt", finish="tool_calls", count=2)
    first["choices"][0]["message"]["reasoning_content"] = "供应商续接数据"
    first["usage"] = {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}
    with local_api([first, completion(text="读到真实资料"), completion(text="继续回答")]) as (
        client,
        requests,
    ):
        model = OpenAIChatModel(client, "local")
        tools = ToolRegistry([READ_FILE], tmp_path)
        with SessionStore(db) as store:
            session = store.create_session(tmp_path)
            first_run = run_agent(model, "读取", tools, store=store, session_id=session)
            before = store.history(session)
        with SessionStore(db) as store:
            assert store.history(session) == before
            second = run_agent(model, "接着说", tools, store=store, session_id=session)
            assert second.session_id == session and second.run_id != first_run.run_id
            record = store.inspect_session(session)
    wire = requests[2]["messages"]
    assert [message["role"] for message in wire] == [
        "system",
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
        "user",
    ]
    assert wire[2]["reasoning_content"] == "供应商续接数据"
    assert [message["tool_call_id"] for message in wire[3:5]] == ["call_0", "call_1"]
    assert record["runs"][0]["messages"][1]["message"]["usage"]["total_tokens"] == 52
    assert record["runs"][0]["result"] == json.loads(json.dumps(asdict(first_run)))
    assert [event["kind"] for event in record["runs"][0]["events"]] == [
        "run_started",
        "model_requested",
        "model_responded",
        "tool_returned",
        "tool_returned",
        "model_requested",
        "model_responded",
        "run_completed",
    ]
    assert all(run["status"] == "completed" and run["finished_at"] for run in record["runs"])


def test_complete_history_and_actual_request_are_saved_without_trimming(tmp_path):
    old_question = "需要保留的原始记录。" * 4000
    with local_api([completion(text="旧回答"), completion(text="新回答")]) as (client, requests):
        with SessionStore(tmp_path / "state.db") as store:
            session = store.create_session(tmp_path)
            model = OpenAIChatModel(client, "local")
            tools = ToolRegistry([], tmp_path)
            run_agent(model, old_question, tools, store=store, session_id=session)
            before = store.inspect_session(session)["runs"][0]
            run_agent(model, "新问题", tools, store=store, session_id=session)
            record = store.inspect_session(session)
    assert record["runs"][0] == before
    wire = requests[1]["messages"]
    assert [message["role"] for message in wire] == ["system", "user", "assistant", "user"]
    assert wire[1]["content"] == old_question
    assert wire[2]["content"] == "旧回答"
    assert wire[3]["content"] == "新问题"
    saved = record["runs"][1]["steps"][0]["request"]
    assert saved["messages"][0] == asdict(SYSTEM)
    assert saved["messages"][1]["content"] == old_question
    assert saved["messages"][2]["text"] == "旧回答"
    assert saved["messages"][3]["content"] == "新问题"
    assert saved["tools"] == []
    assert saved["max_output_tokens"] == requests[1]["max_tokens"] == 512


@pytest.mark.parametrize("reason", ["length", "content_filter", "refusal", "step_limit", "bug"])
def test_failure_records_evidence_and_propagates_original_error(tmp_path, reason):
    response = completion(path="missing.txt", finish="tool_calls")
    if reason in ("length", "content_filter"):
        response["choices"][0]["finish_reason"] = reason
    if reason == "refusal":
        response["choices"][0]["message"]["refusal"] = "拒绝"
    tools = ToolRegistry([READ_FILE], tmp_path)
    if reason == "bug":

        def broken(call):
            raise LookupError("真实程序错误")

        tools.execute = broken
    with local_api([response]) as (client, requests):
        with SessionStore(tmp_path / "state.db") as store:
            session = store.create_session(tmp_path)
            with pytest.raises(LookupError if reason == "bug" else ModelError):
                run_agent(
                    OpenAIChatModel(client, "local"),
                    "读取",
                    tools,
                    max_steps=1,
                    store=store,
                    session_id=session,
                )
            run = store.inspect_session(session)["runs"][0]
            assert store.history(session) == ()
    assert len(requests) == 1
    assert run["status"] == "failed" and run["result"] is None
    assert run["error"]["code"] == ("LookupError" if reason == "bug" else reason)
    assert len(run["messages"]) == (3 if reason == "step_limit" else 2)


def test_policy_still_denies_overwrite_and_receipt_is_persisted(tmp_path):
    (tmp_path / "notes.txt").write_text("原文")
    response = completion(
        name="write_file", arguments={"path": "notes.txt", "content": "替换"}, finish="tool_calls"
    )
    with local_api([response, completion(text="权限拒绝")]) as (client, _):
        with SessionStore(tmp_path / "state.db") as store:
            session = store.create_session(tmp_path)
            result = run_agent(
                OpenAIChatModel(client, "local"),
                "写入",
                ToolRegistry([WRITE_FILE], tmp_path, Policy(allow_create=True)),
                store=store,
                session_id=session,
            )
            stored = store.inspect_session(session)["runs"][0]["messages"][-2]["message"]
    assert result.tool_results[0].content["error"]["code"] == "policy_denied"
    assert stored["content"] == result.tool_results[0].content
    assert (tmp_path / "notes.txt").read_text() == "原文"


def test_unfinished_run_blocks_concurrent_or_restarted_execution(tmp_path):
    db = tmp_path / "state.db"
    with SessionStore(db) as first, SessionStore(db) as second:
        session = first.create_session(tmp_path)
        run_id = first.start_run(session, "未结束的任务")
        with pytest.raises(ValueError, match="未结束"):
            second.start_run(session, "并发任务")
    with SessionStore(db) as restarted:
        with pytest.raises(ValueError, match="未结束"):
            restarted.start_run(session, "重启后新任务")
        assert restarted.inspect_session(session)["runs"][0]["id"] == run_id
        assert restarted.history(session) == ()


def test_terminal_state_and_result_rollback_if_event_commit_fails(tmp_path):
    db = tmp_path / "state.db"
    with SessionStore(db) as store:
        session = store.create_session(tmp_path)
        run_id = store.start_run(session, "任务")
        with sqlite3.connect(db) as connection:
            connection.execute("""
                CREATE TRIGGER fail_event BEFORE INSERT ON events
                WHEN NEW.kind = 'run_completed'
                BEGIN SELECT RAISE(ABORT, 'injected failure'); END
            """)
        with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
            store.finish_run(run_id, result={"answer": "回答", "tool_results": []})
        run = store.inspect_session(session)["runs"][0]
        assert run["status"] == "running" and run["result"] is None
        assert run["finished_at"] is None
        with sqlite3.connect(db) as connection:
            connection.execute("DROP TRIGGER fail_event")
        store.finish_run(run_id, result={"answer": "回答", "tool_results": []})
        with pytest.raises(ValueError, match="正在运行"):
            store.finish_run(run_id, error={"code": "duplicate", "message": "重复结束"})


def test_session_isolation_and_workspace_validation_before_model_creation(tmp_path):
    with SessionStore(tmp_path / "state.db") as store:
        first = store.create_session(tmp_path)
        second = store.create_session(tmp_path)
        run = store.start_run(first, "仅属于第一个会话")
        store.finish_run(run, error={"code": "test", "message": "失败也保留"})
        assert store.inspect_session(second)["runs"] == []
        with pytest.raises(ValueError, match="工作目录"):
            run_task("继续", tmp_path / "elsewhere", Policy(), store=store, session_id=first)
        with pytest.raises(ValueError, match="不存在"):
            store.start_run("unknown", "任务")


def test_cli_continues_session_across_processes_and_query_needs_no_key(tmp_path):
    # 在临时项目副本中启动实际 CLI，让默认记录路径也落在临时目录。
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "learn_agent",
        tmp_path / "learn_agent",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (tmp_path / "examples/workspace").mkdir(parents=True)
    command = [sys.executable, "-m", "learn_agent"]
    with local_api([completion(text="第一轮"), completion(text="第二轮")]) as (client, requests):
        env = dict(
            os.environ,
            DASHSCOPE_API_KEY="local-protocol-test",
            DASHSCOPE_BASE_URL=str(client.base_url),
            DASHSCOPE_MODEL="local",
            DASHSCOPE_USE_ENV_PROXY="false",
        )
        first = subprocess.run(
            command + ["记住这个任务"], cwd=tmp_path, env=env, text=True, capture_output=True
        )
        assert first.returncode == 0, first.stderr
        session = next(
            line.split("：")[1] for line in first.stdout.splitlines() if line.startswith("会话：")
        )
        second = subprocess.run(
            command + ["--session", session, "继续"],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
        )
        assert second.returncode == 0, second.stderr
        env["DASHSCOPE_API_KEY"] = ""
        query = subprocess.run(
            command + ["--show-session", session],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
        )
        assert query.returncode == 0, query.stderr
    assert len(json.loads(query.stdout)["runs"]) == 2
    assert (tmp_path / ".learn-agent/sessions.sqlite3").is_file()
    assert len(requests) == 2
    assert requests[1]["messages"][-2]["content"] == "第一轮"
