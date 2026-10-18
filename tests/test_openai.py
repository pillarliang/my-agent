import os

import pytest

from learn_agent.application import AgentService, AgentSettings
from learn_agent.contracts import Context, Limits
from learn_agent.providers.openai import input_items
from learn_agent.store import SQLiteStore
from tests.helpers import READ_TASK

pytestmark = pytest.mark.live


def test_native_openai_read_tool_roundtrip(live_settings):
    if not os.environ.get("OPENAI_API_KEY") or not os.environ.get("OPENAI_MODEL"):
        pytest.skip("Native OpenAI integration requires OPENAI_API_KEY and OPENAI_MODEL.")
    service = AgentService(
        AgentSettings(
            db=live_settings.db,
            workspace=live_settings.workspace,
            provider="openai",
            limits=Limits(max_steps=5, max_seconds=60, max_output_tokens=512),
        )
    )
    result = service.run(READ_TASK)
    assert result.status == "completed" and "LEARN-42" in result.answer
    assert "notes.txt" in result.answer and result.usage_complete
    store = SQLiteStore(live_settings.db)
    try:
        messages = store.history(result.session_id)[0]
    finally:
        store.close()
    items = input_items(Context("instructions", messages, []))
    calls = {item["call_id"] for item in items if item.get("type") == "function_call"}
    results = {item["call_id"] for item in items if item.get("type") == "function_call_output"}
    assert calls and calls == results
