import subprocess
import sys
from pathlib import Path

import pytest

from learn_agent.store import SQLiteStore, StoreError


def test_session_binding_and_single_writer(store: SQLiteStore):
    store.start("s", "/workspace", "model-a")
    with pytest.raises(StoreError, match="session_busy"):
        store.start("s", "/workspace", "model-a")
    with pytest.raises(StoreError, match="binding_mismatch"):
        store.start("s", "/other-workspace", "model-a")
    with pytest.raises(StoreError, match="binding_mismatch"):
        store.start("s", "/workspace", "model-b")


def test_abandon_checks_actual_child_process_lifecycle(store: SQLiteStore):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    run_id = store.start("s", "/workspace", "model")
    with store.db:
        store.db.execute("UPDATE runs SET pid=? WHERE id=?", (child.pid, run_id))
    try:
        with pytest.raises(StoreError, match="process_still_exists"):
            store.abandon(run_id)
    finally:
        child.terminate()
        child.wait(timeout=5)
    store.abandon(run_id)
    assert store.inspect(run_id)["status"] == "abandoned"
    assert store.history("s") == []
    assert store.start("s", "/workspace", "model") != run_id


def test_database_is_private(tmp_path: Path):
    path = tmp_path / "private/state.sqlite3"
    store = SQLiteStore(path)
    try:
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    finally:
        store.close()
