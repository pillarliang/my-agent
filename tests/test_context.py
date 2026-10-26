"""阶段 4：确定性边界、完整工具链、真实 SDK 请求与会话原文保留。"""

import json
from copy import deepcopy

import pytest
from protocol_support import completion, local_api

from learn_agent.context import (
    ContextBudget,
    ContextOverflowError,
    HistoryTurn,
    InputMeter,
    select_context,
)
from learn_agent.models import ModelRequest, ModelResponse, OpenAIChatModel, TextMessage
from learn_agent.models.openai_chat import estimate_input_tokens
from learn_agent.runner import SYSTEM, run_agent
from learn_agent.session import SessionLog, read_session
from learn_agent.tools import READ_FILE, ToolRegistry


def turn(turn_id, question, answer="x"):
    return HistoryTurn(
        turn_id, (TextMessage("user", question), ModelResponse(answer, (), "stop", "local"))
    )


def text_size(request):
    return sum(
        len(message.content if isinstance(message, TextMessage) else message.text or "")
        for message in request.messages
    )


def choose(history, limit, **kwargs):
    return select_context(
        system=TextMessage("system", "base"),
        history=history,
        current=(TextMessage("user", "task"),),
        budget=ContextBudget(max_input_tokens=limit),
        meter=InputMeter(text_size, "test_characters"),
        **kwargs,
    )


@pytest.mark.parametrize(
    "limit, kept, dropped, used",
    [
        (32, ("T1", "T2", "T3"), (), 31),
        (31, ("T1", "T2", "T3"), (), 31),
        (30, ("T1", "T3"), ("T2",), 19),
        (12, ("T1",), ("T2", "T3"), 12),
    ],
)
def test_whole_turn_selection_and_pinning(limit, kept, dropped, used):
    history = (turn("T1", "UTC"), turn("T2", "b" * 11), turn("T3", "c" * 6))
    original = deepcopy(history)
    selected = choose(history, limit, required_turn_ids=frozenset({"T1"}))
    assert (selected.decision.kept, selected.decision.dropped, selected.decision.used) == (
        kept,
        dropped,
        used,
    )
    assert selected.request.messages == (
        TextMessage("system", "base"),
        *(message for item in history if item.turn_id in kept for message in item.messages),
        TextMessage("user", "task"),
    )
    assert history == original


def test_remeasures_complete_request_instead_of_subtracting_turn_sizes():
    seen = []

    def nonlinear(request):
        seen.append(request)
        return len(request.messages) ** 2

    selected = select_context(
        system=SYSTEM,
        history=(turn("T1", "first"), turn("T2", "second")),
        current=(TextMessage("user", "now"),),
        budget=ContextBudget(max_input_tokens=16),
        meter=InputMeter(nonlinear, "test_squared_message_count"),
    )
    assert [len(request.messages) for request in seen] == [6, 4]
    assert selected.decision.kept == ("T2",)
    assert selected.decision.used == 16


def test_budget_subtracts_output_and_margin_before_applying_input_cap():
    assert ContextBudget(100, 20, 10).input_budget == 70
    assert ContextBudget(100, 20, 10, 60).input_budget == 60
    assert ContextBudget(100, 20, 10, 80).input_budget == 70


@pytest.mark.parametrize(
    "settings",
    [
        {"context_window": 0},
        {"max_output_tokens": 0},
        {"safety_margin": -1},
        {"max_input_tokens": 0},
        {"context_window": 512, "safety_margin": 0},
        {"context_window": 1000},
        {"context_window": True},
        {"max_input_tokens": 1.5},
    ],
)
def test_invalid_budget_is_rejected(settings):
    with pytest.raises(ValueError):
        ContextBudget(**settings)


@pytest.mark.parametrize("value", [None, 0, -1, float("nan"), 1.5, True])
def test_invalid_meter_result_is_not_treated_as_zero(value):
    with pytest.raises(ValueError, match="计量"):
        select_context(
            system=SYSTEM,
            history=(),
            current=(TextMessage("user", "x"),),
            meter=InputMeter(lambda request: value, "invalid"),
        )


def test_missing_pin_duplicate_id_and_incomplete_memory_history_are_rejected():
    with pytest.raises(ValueError, match="不存在"):
        choose((turn("T1", "x"),), 100, required_turn_ids=frozenset({"typo"}))
    with pytest.raises(ValueError, match="重复"):
        choose((turn("same", "x"), turn("same", "y")), 100)
    with pytest.raises(ValueError, match="不完整"):
        HistoryTurn("unfinished", (TextMessage("user", "x"),))
    with pytest.raises(ValueError, match="用户问题"):
        HistoryTurn("system", (SYSTEM, ModelResponse("x", (), "stop", "local")))


def test_required_overflow_has_measurement_and_does_not_mutate_history():
    history = (turn("T1", "UTC"), turn("T2", "b" * 11))
    original = deepcopy(history)
    with pytest.raises(ContextOverflowError) as caught:
        choose(history, 11, required_turn_ids=frozenset({"T1"}))
    decision = caught.value.decision
    assert (decision.budget, decision.used, decision.unit) == (11, 12, "test_characters")
    assert decision.kept == ("T1",)
    assert decision.dropped == ("T2",)
    assert history == original


def test_sdk_receives_only_selected_turns_and_matching_output_reservation(tmp_path):
    with SessionLog(tmp_path / "sessions", workspace=tmp_path) as session:
        ids = []
        for question, answer in [("用中文，时间统一写 UTC", "好的"), ("旧草稿" * 1000, "废稿")]:
            turn_id = session.start_turn(question)
            session.append_message(turn_id, ModelResponse(answer, (), "stop", "local"))
            session.finish_turn(turn_id)
            ids.append(turn_id)
        original = session.path.read_bytes()
        history = session.history_turns()
        required_request = ModelRequest(
            (SYSTEM, *history[0].messages, TextMessage("user", "继续")), (), 73
        )
        limit = estimate_input_tokens(required_request)
        with local_api([completion(text="通知")]) as (client, requests):
            result = run_agent(
                OpenAIChatModel(client, "local"),
                "继续",
                ToolRegistry([], tmp_path),
                session=session,
                required_turn_ids=frozenset({ids[0]}),
                context_budget=ContextBudget(max_output_tokens=73, max_input_tokens=limit),
            )
        assert session.path.read_bytes().startswith(original)
        assert session.history_turns()[:2] == history
        session_id = session.header.session_id
    decision = result.context_decisions[0]
    assert decision.kept == (ids[0],) and decision.dropped == (ids[1],)
    assert requests[0]["max_tokens"] == 73
    assert [m["content"] for m in requests[0]["messages"]] == [
        SYSTEM.content,
        "用中文，时间统一写 UTC",
        "好的",
        "继续",
    ]
    assert decision.used == len(
        json.dumps({"messages": requests[0]["messages"]}, ensure_ascii=False).encode()
    )
    with SessionLog(tmp_path / "sessions", session_id=session_id) as reopened:
        assert reopened.history_turns()[:2] == history


@pytest.mark.parametrize("pin", [False, True])
def test_multi_tool_history_keeps_pairs_failures_and_continuation(tmp_path, pin):
    response = completion(path="missing.txt", count=2, finish="tool_calls")
    response["choices"][0]["message"]["reasoning_content"] = "原生材料" * 50
    with local_api([response, completion(text="文件不存在"), completion(text="继续")]) as (
        client,
        requests,
    ):
        model = OpenAIChatModel(client, "local")
        tools = ToolRegistry([READ_FILE], tmp_path)
        with SessionLog(tmp_path / "sessions", workspace=tmp_path) as session:
            first = run_agent(model, "读取", tools, session=session)
            history = session.history_turns()
            original = deepcopy(history)
            without_history = ModelRequest(
                (SYSTEM, TextMessage("user", "下一问")), tuple(tools.specs)
            )
            with_history = ModelRequest(
                (SYSTEM, *session.history(), TextMessage("user", "下一问")), tuple(tools.specs)
            )
            limit = estimate_input_tokens(with_history if pin else without_history)
            second = run_agent(
                model,
                "下一问",
                tools,
                session=session,
                required_turn_ids=frozenset({first.turn_id}) if pin else frozenset(),
                context_budget=ContextBudget(max_input_tokens=limit),
            )
            assert session.history_turns()[:1] == original
    sent = requests[-1]
    if pin:
        assert [m["role"] for m in sent["messages"]] == [
            "system",
            "user",
            "assistant",
            "tool",
            "tool",
            "assistant",
            "user",
        ]
        assert sent["messages"][2]["reasoning_content"] == "原生材料" * 50
        assert [m["tool_call_id"] for m in sent["messages"][3:5]] == ["call_0", "call_1"]
        assert all(not json.loads(m["content"])["ok"] for m in sent["messages"][3:5])
    else:
        assert [m["role"] for m in sent["messages"]] == ["system", "user"]
    actual_input = {key: sent[key] for key in ("messages", "tools", "parallel_tool_calls")}
    assert second.context_decisions[0].used == len(
        json.dumps(actual_input, ensure_ascii=False).encode()
    )


@pytest.mark.parametrize("overflow", [False, True])
def test_tool_growth_rechecks_budget_and_preserves_receipts(tmp_path, overflow):
    content = "z" * (8000 if overflow else 800)
    (tmp_path / "notes.txt").write_text(content)
    tools = ToolRegistry([READ_FILE], tmp_path)
    with local_api(
        [completion(path="notes.txt", finish="tool_calls"), completion(text="done")]
    ) as (client, requests):
        with SessionLog(tmp_path / "sessions", workspace=tmp_path) as session:
            old = session.start_turn("old" * 600)
            session.append_message(old, ModelResponse("old answer", (), "stop", "local"))
            session.finish_turn(old)
            before = session.path.read_bytes()
            initial = ModelRequest(
                (SYSTEM, *session.history(), TextMessage("user", "read")), tuple(tools.specs)
            )
            budget = ContextBudget(max_input_tokens=estimate_input_tokens(initial))
            if overflow:
                with pytest.raises(ContextOverflowError) as caught:
                    run_agent(
                        OpenAIChatModel(client, "local"),
                        "read",
                        tools,
                        session=session,
                        context_budget=budget,
                    )
                assert caught.value.decision.dropped == (old,)
                assert caught.value.decision.used > caught.value.decision.budget
            else:
                result = run_agent(
                    OpenAIChatModel(client, "local"),
                    "read",
                    tools,
                    session=session,
                    context_budget=budget,
                )
                assert result.context_decisions[0].kept == (old,)
                assert result.context_decisions[1].dropped == (old,)
                assert [m["role"] for m in requests[1]["messages"]] == [
                    "system",
                    "user",
                    "assistant",
                    "tool",
                ]
            assert session.path.read_bytes().startswith(before)
            session_id = session.header.session_id
    assert len(requests) == (1 if overflow else 2)
    records = read_session(tmp_path / "sessions", session_id)["records"]
    receipt = next(r for r in records if r["payload"].get("kind") == "tool_result")
    assert receipt["payload"]["value"]["content"]["data"]["text"] == content
    assert records[-1]["payload"]["status"] == ("failed" if overflow else "completed")
    if overflow:
        assert records[-1]["payload"]["error"]["code"] == "context_overflow"


def test_preflight_overflow_and_meter_failure_send_no_requests(tmp_path):
    class UnexpectedModel:
        def invoke(self, request):
            pytest.fail("预算检查失败时不能请求模型")

    def broken_meter(request):
        raise RuntimeError("计量器失败")

    tools = ToolRegistry([], tmp_path)
    with SessionLog(tmp_path / "sessions", workspace=tmp_path) as session:
        with pytest.raises(ContextOverflowError):
            run_agent(
                UnexpectedModel(),
                "hello",
                tools,
                session=session,
                context_budget=ContextBudget(max_input_tokens=1),
            )
        assert session.history() == ()
        with pytest.raises(RuntimeError, match="计量器失败"):
            run_agent(
                UnexpectedModel(),
                "hello",
                tools,
                session=session,
                input_meter=InputMeter(broken_meter, "broken"),
            )
        assert session.records[-1].payload["status"] == "failed"


def test_runner_accepts_memory_history_without_storage(tmp_path):
    with local_api([completion(text="done")]) as (client, requests):
        result = run_agent(
            OpenAIChatModel(client, "local"),
            "now",
            ToolRegistry([], tmp_path),
            history=(turn("T1", "previous"),),
            required_turn_ids=frozenset({"T1"}),
        )
    assert result.session_id is None
    assert requests[0]["messages"][1]["content"] == "previous"
    assert result.context_decisions[0].kept == ("T1",)


def test_cli_budget_options_and_pinning(tmp_path, monkeypatch):
    from test_application import configure_local_model

    from learn_agent import __main__ as entry

    monkeypatch.setattr(entry, "WORKSPACE", tmp_path)
    with SessionLog(entry.SESSIONS_DIRECTORY, workspace=tmp_path) as session:
        first = session.start_turn("UTC")
        session.append_message(first, ModelResponse("okay", (), "stop", "local"))
        session.finish_turn(first)
        session_id = session.header.session_id
    with local_api([completion(text="done")]) as (client, requests):
        configure_local_model(monkeypatch, client)
        assert (
            entry.main(
                [
                    "--session",
                    session_id,
                    "--pin-turn",
                    first,
                    "--context-window",
                    "6000",
                    "--max-input-tokens",
                    "4500",
                    "--max-output-tokens",
                    "123",
                    "--safety-margin",
                    "500",
                    "continue",
                ]
            )
            == 0
        )
    assert requests[0]["max_tokens"] == 123
    assert requests[0]["messages"][1]["content"] == "UTC"
    with pytest.raises(SystemExit):
        entry.main(["--context-window", "1", "hello"])
    with pytest.raises(SystemExit):
        entry.main(["--pin-turn", first, "hello"])
