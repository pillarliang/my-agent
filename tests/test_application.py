import pytest

from learn_agent.application import AgentService, RecordService
from learn_agent.contracts import Message, RunResult
from learn_agent.store import SQLiteStore
from tests.helpers import READ_TASK


@pytest.mark.live
def test_history_is_shared_between_service_instances(live_settings):
    first = AgentService(live_settings)
    session = first.create_session()["id"]
    result = first.run(READ_TASK, session_id=session)
    assert result.status == "completed"
    second = AgentService(live_settings)
    assert second.list_sessions()[0]["title"] == READ_TASK
    assert second.get_session(session)["runs"][0]["result"]["answer"] == result.answer
    assert RecordService(live_settings.db).inspect(result.run_id)["status"] == "completed"


def test_history_paginates_cancelled_records_without_model_calls(live_settings):
    service = AgentService(live_settings)
    session = service.create_session()["id"]
    store = SQLiteStore(live_settings.db)
    try:
        for index in range(23):
            run_id = store.start(session, str(service.workspace.root), service.model_key)
            store.finish(
                RunResult(
                    run_id=run_id, session_id=session, status="cancelled", usage_complete=False
                ),
                [Message(role="user", text=str(index))],
            )
    finally:
        store.close()
    latest = service.get_session(session)
    older = service.get_session(session, before=latest["next_before"])
    assert len(latest["runs"]) == 20 and len(older["runs"]) == 3
    assert older["next_before"] is None
    assert older["runs"][0]["messages"][0]["text"] == "0"
    assert latest["runs"][-1]["messages"][0]["text"] == "22"
    assert all("provider_items" not in run["messages"][0] for run in latest["runs"])
