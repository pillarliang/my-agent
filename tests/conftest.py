"""CLI 测试的会话记录写入临时目录，不污染项目中的用户历史。"""

import pytest


@pytest.fixture(autouse=True)
def isolate_cli_sessions(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(
        "learn_agent.__main__.SESSIONS_DIRECTORY", tmp_path_factory.mktemp("cli-sessions")
    )
