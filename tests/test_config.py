from pathlib import Path

import pytest

from learn_agent.application import AgentService, AgentSettings
from learn_agent.config import dashscope_endpoint, load_environment


def test_dotenv_loads_exports_without_overriding_environment(tmp_path: Path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        'export TEST_AGENT_FILE="from-file"\nTEST_AGENT_SHELL=from-file\n'
        'TEST_AGENT_LITERAL="${TEST_AGENT_SHELL}"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("TEST_AGENT_FILE", raising=False)
    monkeypatch.delenv("TEST_AGENT_LITERAL", raising=False)
    monkeypatch.setenv("TEST_AGENT_SHELL", "from-shell")
    load_environment(path)
    import os

    assert os.environ["TEST_AGENT_FILE"] == "from-file"
    assert os.environ["TEST_AGENT_SHELL"] == "from-shell"
    assert os.environ["TEST_AGENT_LITERAL"] == "${TEST_AGENT_SHELL}"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://example.test/v1",
        "https://key:secret@example.test/v1",
        "https://example.test/v1?secret=x",
        "https://example.test/#secret",
    ],
)
def test_endpoint_does_not_accept_credentials_or_query(monkeypatch, url):
    monkeypatch.setenv("DASHSCOPE_BASE_URL", url)
    with pytest.raises(ValueError, match="HTTPS"):
        dashscope_endpoint()


def test_endpoint_identity_is_part_of_session_binding(tmp_path: Path, monkeypatch, live_settings):
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://one.example.test/v1")
    settings = AgentSettings(
        db=tmp_path / "state.sqlite3",
        workspace=tmp_path,
        provider="dashscope",
        model="qwen3.8-flash",
    )

    first = AgentService(settings)
    session = first.create_session()["id"]
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://two.example.test/v1")
    second = AgentService(settings)
    assert not second.get_session(session)["compatible"]
    assert "one.example.test" not in first.model_key


def test_default_provider_and_model(monkeypatch):
    from learn_agent.cli import parser
    from learn_agent.config import resolve_model

    monkeypatch.delenv("DASHSCOPE_MODEL", raising=False)
    assert parser().parse_args(["run", "task"]).provider == "dashscope"
    assert AgentSettings().model == "qwen3.8-flash"
    monkeypatch.setenv("DASHSCOPE_MODEL", "configured-model")
    assert resolve_model("dashscope") == "configured-model"
    assert resolve_model("dashscope", "explicit-model") == "explicit-model"
