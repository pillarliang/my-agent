import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from learn_agent.application import AgentSettings
from learn_agent.config import load_environment
from learn_agent.contracts import Limits
from learn_agent.providers.dashscope import DashScopeModel
from learn_agent.store import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]
load_environment(ROOT / ".env")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    instance = SQLiteStore(tmp_path / "data/state.sqlite3")
    yield instance
    instance.close()


@pytest.fixture
def live_settings(tmp_path: Path) -> AgentSettings:
    if not os.environ.get("DASHSCOPE_API_KEY") or not os.environ.get("DASHSCOPE_BASE_URL"):
        pytest.skip("Set DASHSCOPE_API_KEY and DASHSCOPE_BASE_URL for real model integration.")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text(
        "学习目标：理解 Agent Loop。\n验收标记：LEARN-42\n", encoding="utf-8"
    )
    return AgentSettings(
        db=tmp_path / "state.sqlite3",
        workspace=workspace,
        allow_write=True,
        limits=Limits(max_steps=5, max_seconds=60, max_output_tokens=512),
    )


@pytest.fixture
def real_model(live_settings: AgentSettings) -> Iterator[DashScopeModel]:
    from learn_agent.config import dashscope_endpoint, dashscope_use_env_proxy

    model = DashScopeModel(
        live_settings.model,
        api_key=os.environ["DASHSCOPE_API_KEY"],
        base_url=dashscope_endpoint(),
        use_env_proxy=dashscope_use_env_proxy(),
    )
    try:
        yield model
    finally:
        model.close()
