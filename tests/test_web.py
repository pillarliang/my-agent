import importlib
import json
import os
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from learn_agent.application import AgentService
from tests.helpers import READ_TASK, REPORT_TASK

web = pytest.importorskip("learn_agent.web")
TestClient = importlib.import_module("fastapi.testclient").TestClient
HEADERS = {"X-Learn-Agent": "web"}


@pytest.fixture
def service(live_settings):
    return AgentService(replace(live_settings, verify_file="report.md", contains="LEARN-42"))


@pytest.fixture
def client(service):
    with TestClient(web.create_app(service), base_url="http://localhost") as instance:
        yield instance


def complete(client, run_id):
    deadline = time.monotonic() + 70
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.05)
    raise AssertionError("Real model run did not finish within the test deadline.")


def start(client, question=REPORT_TASK, session=None):
    session = session or client.post("/api/sessions", headers=HEADERS).json()["id"]
    response = client.post(
        f"/api/sessions/{session}/runs", headers=HEADERS, json={"question": question}
    )
    assert response.status_code == 202, response.text
    return session, response.json()["run_id"]


def test_page_assets_and_config_do_not_expose_secrets(client):
    page = client.get("/")
    assert page.status_code == 200 and "学习工作台" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/style.css").status_code == 200
    config = client.get("/api/config")
    assert config.json()["provider"] == "dashscope"
    leaked = os.environ["DASHSCOPE_API_KEY"] in config.text
    assert not leaked


@pytest.mark.live
def test_web_chat_tools_history_and_cli_share_state(client, service):
    session, run_id = start(client)
    run = complete(client, run_id)
    assert run["status"] == "completed" and run["result"]["verification"]["status"] == "passed"
    assert run["result"]["outcome"]["status"] == "passed"
    assert run["result"]["verification"]["checks"][0]["target"] == "report.md"
    assert (service.workspace.root / "report.md").exists()
    history = client.get(f"/api/sessions/{session}").json()
    assert history["runs"][0]["messages"][0]["text"] == REPORT_TASK
    cli = subprocess.run(
        [sys.executable, "-m", "learn_agent.cli", "--db", str(service.db), "inspect", run_id],
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(cli.stdout)["status"] == "completed"
    assert AgentService(service.settings).get_session(session)["runs"][0]["id"] == run_id


@pytest.mark.live
def test_cli_created_session_appears_in_web(client, service):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "learn_agent.cli",
            "--db",
            str(service.db),
            "run",
            READ_TASK,
            "--workspace",
            str(service.workspace.root),
            "--session",
            "cli-flash-lesson",
            "--json",
            "--max-steps",
            "5",
            "--max-output-tokens",
            "512",
            "--max-seconds",
            "60",
        ],
        text=True,
        capture_output=True,
        check=True,
        timeout=75,
    )
    run_id = json.loads(result.stdout)["run_id"]
    sessions = client.get("/api/sessions").json()["sessions"]
    assert any(
        session["id"] == "cli-flash-lesson" and session["compatible"] for session in sessions
    )
    assert client.get(f"/api/runs/{run_id}").json()["messages"][0]["text"] == READ_TASK


@pytest.mark.live
def test_events_resume_without_tool_reexecution(client, service):
    _, run_id = start(client)
    assert complete(client, run_id)["status"] == "completed"
    first = client.get(f"/api/runs/{run_id}/events")
    assert first.headers["content-type"].startswith("text/event-stream")
    assert '"name": "tool.started"' in first.text and '"name": "tool.finished"' in first.text
    assert "event: end" in first.text
    ids = [int(line[4:]) for line in first.text.splitlines() if line.startswith("id: ")]
    resumed = client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(ids[-2])})
    assert f"id: {ids[-1]}\n" in resumed.text and f"id: {ids[0]}\n" not in resumed.text
    assert service.events(run_id, 0)["events"][-1]["seq"] == ids[-1]


@pytest.mark.parametrize(
    "payload",
    [
        {"question": ""},
        {"question": "  "},
        {"question": 42},
        {"question": "x", "allow_write": True},
        {"question": "x" * 16001},
    ],
)
def test_invalid_chat_body_is_rejected(client, payload):
    session = client.post("/api/sessions", headers=HEADERS).json()["id"]
    response = client.post(f"/api/sessions/{session}/runs", headers=HEADERS, json=payload)
    assert response.status_code == 422
    assert not client.get(f"/api/sessions/{session}").json()["runs"]


def test_local_origin_boundary(client):
    assert client.post("/api/sessions").status_code == 403
    assert (
        client.post(
            "/api/sessions", headers={**HEADERS, "Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        client.post("/api/sessions", headers={**HEADERS, "Origin": "http://localhost"}).status_code
        == 201
    )
    assert client.get("/api/config", headers={"Host": "evil.example"}).status_code == 400


def test_missing_resources_and_invalid_cursor(client, service):
    assert client.get("/api/sessions/missing").status_code == 404
    assert client.get("/api/runs/missing/events").status_code == 404
    session = service.create_session()["id"]
    from learn_agent.store import SQLiteStore

    store = SQLiteStore(service.db)
    try:
        run_id = store.start(session, str(service.workspace.root), service.model_key)
        assert (
            client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "bad"}).status_code
            == 400
        )
        store.fail_running(run_id)
    finally:
        store.close()


@pytest.mark.live
def test_real_model_does_not_block_http_and_duplicate_run_is_rejected(client):
    session, run_id = start(client)
    assert client.get("/api/config").status_code == 200
    if client.get(f"/api/runs/{run_id}").json()["status"] != "running":
        pytest.skip("Real task completed before the overlap check.")
    duplicate = client.post(
        f"/api/sessions/{session}/runs", headers=HEADERS, json={"question": "duplicate"}
    )
    assert duplicate.status_code == 409 and duplicate.json()["detail"] == "session_busy"
    assert complete(client, run_id)["status"] == "completed"


def test_incompatible_session_remains_readable_but_cannot_run(client, service, tmp_path):
    other = AgentService(replace(service.settings, workspace=tmp_path))
    session = other.create_session()["id"]
    assert not client.get(f"/api/sessions/{session}").json()["compatible"]
    assert (
        client.post(
            f"/api/sessions/{session}/runs", headers=HEADERS, json={"question": "x"}
        ).status_code
        == 409
    )
