"""所有入口检查使用临时数据库，不向开发者的会话记录写入测试任务。"""

import pytest


@pytest.fixture(autouse=True)
def isolated_session_database(tmp_path, monkeypatch):
    monkeypatch.setattr("learn_agent.__main__.DATABASE", tmp_path / ".records/state.db")
