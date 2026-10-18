from pathlib import Path

import pytest

from learn_agent.context import build_context
from learn_agent.contracts import ContextOverflow, Message, ToolCall, ToolResult
from learn_agent.policy import Workspace
from learn_agent.tools import ToolExecutor


def test_evict_whole_turns_without_mutating_history():
    history = [[Message(role="user", text="x" * 20_000), Message(role="assistant", text="old")]]
    current = [
        Message(role="user", text="new"),
        Message(role="assistant", calls=[ToolCall(id="c", name="read_file", arguments="{}")]),
        Message(role="tool", call_id="c", result=ToolResult(ok=True, code="ok")),
    ]
    context = build_context(history, current, [], 3000)
    assert context.omitted_turns == 1
    assert context.messages[-3:] == current
    assert len(history[0][0].text) == 20_000


def test_current_turn_is_never_silently_truncated():
    with pytest.raises(ContextOverflow):
        build_context([], [Message(role="user", text="中" * 10_000)], [], 3000)


def test_tool_definitions_count_toward_size(tmp_path: Path):
    specs = ToolExecutor(Workspace(tmp_path)).specs
    current = [Message(role="user", text="x")]
    size = build_context([], current, [], 100_000).input_bytes
    with pytest.raises(ContextOverflow):
        build_context([], current, specs, size)
