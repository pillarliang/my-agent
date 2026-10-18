import pytest

from learn_agent.contracts import Limits
from learn_agent.policy import Workspace
from learn_agent.runner import Runner
from learn_agent.store import SQLiteStore
from learn_agent.tools import ToolExecutor
from learn_agent.verification import FileContainsVerifier
from tests.helpers import READ_TASK, REPORT_TASK

pytestmark = pytest.mark.live


def test_real_model_creates_report_and_records_event_order(live_settings, real_model, store):
    workspace = Workspace(live_settings.workspace, allow_write=True)
    result = Runner(
        real_model,
        ToolExecutor(workspace),
        store,
        model_key=f"dashscope:{live_settings.model}",
        limits=live_settings.limits,
        verifier=FileContainsVerifier(workspace, "report.md", "LEARN-42"),
    ).run(REPORT_TASK, session_id="real-report")
    assert result.status == "completed" and result.verification.status == "passed"
    assert result.outcome.status == "passed"
    assert {item.tool for item in result.tool_evidence} >= {"read_file", "create_file"}
    assert result.verification.checks[0].target == "report.md"
    assert "LEARN-42" in (workspace.root / "report.md").read_text()
    assert result.usage_complete and result.input_tokens > 0
    names = [event["name"] for event in store.inspect(result.run_id)["events"]]
    assert names.index("tool.started") < names.index("tool.finished") < names.index("run.finished")
    assert len(store.history("real-report")) == 1


def test_step_limit_after_real_tool_request(live_settings, real_model, store):
    result = Runner(
        real_model,
        ToolExecutor(Workspace(live_settings.workspace)),
        store,
        model_key="dashscope:step-limit",
        limits=Limits(max_steps=1, max_output_tokens=512),
    ).run(READ_TASK, session_id="one-step")
    assert result.status == "step_limit"
    assert any(event["name"] == "tool.finished" for event in store.inspect(result.run_id)["events"])


def test_context_limit_stops_before_network(live_settings, real_model, store):
    result = Runner(
        real_model,
        ToolExecutor(Workspace(live_settings.workspace)),
        store,
        model_key="dashscope:context-limit",
        limits=Limits(max_input_bytes=1),
    ).run(READ_TASK, session_id="context-limit")
    assert result.status == "context_limit" and result.steps == 0
    assert not any(
        event["name"] == "model.started" for event in store.inspect(result.run_id)["events"]
    )


def test_final_model_answer_does_not_replace_verifier(live_settings, real_model, store):
    workspace = Workspace(live_settings.workspace)
    result = Runner(
        real_model,
        ToolExecutor(workspace),
        store,
        model_key="dashscope:verification",
        limits=live_settings.limits,
        verifier=FileContainsVerifier(workspace, "missing.md", "required"),
    ).run("用一句话解释 Agent Loop。不要创建文件。", session_id="verify")
    assert result.answer and result.status == "verification_failed"
    assert result.outcome.status == "failed"
    assert result.verification.status == "failed" and not store.history("verify")


def test_history_survives_reopening_with_real_model(live_settings, real_model):
    first = SQLiteStore(live_settings.db)
    tools = ToolExecutor(Workspace(live_settings.workspace))
    try:
        result = Runner(real_model, tools, first, model_key="dashscope:history").run(
            "请记住本会话的标记 MEMORY-CHECK-27，仅回复已记住。", session_id="history"
        )
        assert result.status == "completed"
    finally:
        first.close()
    second = SQLiteStore(live_settings.db)
    try:
        result = Runner(real_model, tools, second, model_key="dashscope:history").run(
            "上一条用户请求中的标记是什么？", session_id="history"
        )
        assert result.status == "completed" and "MEMORY-CHECK-27" in result.answer
    finally:
        second.close()


def test_real_clock_budget_stops_before_network(live_settings, real_model, store):
    result = Runner(
        real_model,
        ToolExecutor(Workspace(live_settings.workspace)),
        store,
        model_key="dashscope:time-limit",
        limits=Limits(max_seconds=1e-9),
    ).run(READ_TASK, session_id="time-limit")
    assert result.status == "time_limit" and result.steps == 0


def test_contains_check_only_proves_current_file_state(live_settings, real_model, store):
    path = live_settings.workspace / "report.md"
    path.write_text("LEARN-42", encoding="utf-8")
    workspace = Workspace(live_settings.workspace)
    result = Runner(
        real_model,
        ToolExecutor(workspace),
        store,
        model_key="dashscope:contains",
        limits=live_settings.limits,
        verifier=FileContainsVerifier(workspace, "report.md", "LEARN-42"),
    ).run("用一句话解释 Agent。不要读取或创建文件。", session_id="contains")
    assert result.status == "completed" and result.verification.status == "passed"
    assert path.read_text() == "LEARN-42"
    assert result.outcome.summary == "已通过指定检查"
    assert not any(item.tool == "create_file" and item.ok for item in result.tool_evidence)
