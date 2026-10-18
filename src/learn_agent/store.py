"""SQLite audit records and complete-turn history. No automatic run resume."""

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from learn_agent.contracts import Message, RunResult


class StoreError(Exception):
    pass


class SQLiteStore:
    def __init__(self, path: Path, *, initialize: bool = True) -> None:
        if not initialize and not path.is_file():
            raise StoreError("database_not_found")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        if self.db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
            self.db.close()
            raise StoreError("unsupported_database_version")
        self.db.execute("PRAGMA foreign_keys = ON")
        if not initialize:
            return
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL, model_key TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                status TEXT NOT NULL, pid INTEGER NOT NULL, created_at TEXT NOT NULL,
                messages TEXT NOT NULL DEFAULT '[]', result TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_run
                ON runs(session_id) WHERE status = 'running';
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
                at TEXT NOT NULL, name TEXT NOT NULL, data TEXT NOT NULL
            );
            PRAGMA user_version = 1;
        """)

    def close(self) -> None:
        self.db.close()

    def create_session(self, session_id: str, workspace: str, model_key: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)", (session_id, workspace, model_key)
            )

    def sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT s.*,
                (SELECT status FROM runs WHERE session_id=s.id ORDER BY rowid DESC LIMIT 1) status,
                (SELECT created_at FROM runs WHERE session_id=s.id
                    ORDER BY rowid DESC LIMIT 1) updated_at,
                (SELECT messages FROM runs WHERE session_id=s.id
                    ORDER BY rowid LIMIT 1) first_messages
            FROM sessions s ORDER BY s.rowid DESC LIMIT ?
        """,
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            messages = json.loads(row["first_messages"] or "[]")
            title = next((m["text"] for m in messages if m["role"] == "user"), "新会话")
            result.append(
                {
                    "id": row["id"],
                    "workspace": row["workspace"],
                    "model_key": row["model_key"],
                    "status": row["status"] or "idle",
                    "updated_at": row["updated_at"],
                    "title": title[:80],
                }
            )
        return result

    def session(
        self, session_id: str, *, before: int | None = None, limit: int = 20
    ) -> dict[str, Any]:
        session = self.db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if session is None:
            raise StoreError("session_not_found")
        rows = self.db.execute(
            """
            SELECT rowid AS position, * FROM runs
            WHERE session_id=? AND (? IS NULL OR rowid<?)
            ORDER BY rowid DESC LIMIT ?
        """,
            (session_id, before, before, limit + 1),
        ).fetchall()
        page = rows[:limit]
        return {
            **dict(session),
            "runs": [self._public_run(row) for row in reversed(page)],
            "next_before": page[-1]["position"] if len(rows) > limit else None,
        }

    @staticmethod
    def _public_run(row: sqlite3.Row) -> dict[str, Any]:
        # HTTP clients get display data, never opaque reasoning continuation items.
        messages = [
            Message.model_validate(m).model_dump(exclude={"provider_items"})
            for m in json.loads(row["messages"])
        ]
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "messages": messages,
            # Recompute conclusions for old records too; no schema migration is needed.
            "result": RunResult.model_validate_json(row["result"]).model_dump()
            if row["result"]
            else None,
        }

    def run_detail(self, run_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise StoreError("run_not_found")
        return self._public_run(row)

    def events_since(self, run_id: str, after: int, limit: int = 200) -> dict[str, Any]:
        run = self.db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise StoreError("run_not_found")
        rows = self.db.execute(
            """
            SELECT seq,at,name,data FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?
        """,
            (run_id, after, limit),
        ).fetchall()
        return {
            "status": run["status"],
            "events": [{**dict(row), "data": json.loads(row["data"])} for row in rows],
        }

    def fail_running(self, run_id: str) -> None:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is not None and row["status"] == "running":
            messages = [Message.model_validate(item) for item in json.loads(row["messages"])]
            self.finish(
                RunResult(
                    run_id=run_id,
                    session_id=row["session_id"],
                    status="internal_error",
                    usage_complete=False,
                ),
                messages,
            )

    def start(self, session_id: str, workspace: str, model_key: str) -> str:
        run_id = uuid4().hex
        try:
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO sessions VALUES (?, ?, ?)",
                    (session_id, workspace, model_key),
                )
                row = self.db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
                if row["workspace"] != workspace or row["model_key"] != model_key:
                    raise StoreError("session_binding_mismatch: use a new session")
                self.db.execute(
                    "INSERT INTO runs (id,session_id,status,pid,created_at) VALUES (?,?,?,?,?)",
                    (run_id, session_id, "running", os.getpid(), datetime.now(UTC).isoformat()),
                )
        except sqlite3.IntegrityError as exc:
            raise StoreError("session_busy: inspect the active run") from exc
        return run_id

    def history(self, session_id: str) -> list[list[Message]]:
        rows = self.db.execute(
            "SELECT messages FROM runs WHERE session_id=? AND status='completed' ORDER BY rowid",
            (session_id,),
        )
        return [[Message.model_validate(item) for item in json.loads(row[0])] for row in rows]

    def save_messages(self, run_id: str, messages: list[Message]) -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET messages=? WHERE id=?",
                (json.dumps([m.model_dump() for m in messages]), run_id),
            )

    def event(self, run_id: str, name: str, data: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO events (run_id,at,name,data) VALUES (?,?,?,?)",
                (run_id, datetime.now(UTC).isoformat(), name, json.dumps(data)),
            )

    def finish(self, result: RunResult, messages: list[Message]) -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET status=?,messages=?,result=? WHERE id=?",
                (
                    result.status,
                    json.dumps([m.model_dump() for m in messages]),
                    result.model_dump_json(),
                    result.run_id,
                ),
            )
            self.db.execute(
                "INSERT INTO events (run_id,at,name,data) VALUES (?,?,?,?)",
                (
                    result.run_id,
                    datetime.now(UTC).isoformat(),
                    "run.finished",
                    json.dumps(
                        {"status": result.status, "verification": result.verification.status}
                    ),
                ),
            )

    def inspect(self, run_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT id,session_id,status,pid,created_at,result FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise StoreError("run_not_found")
        events = self.db.execute(
            "SELECT seq,at,name,data FROM events WHERE run_id=? ORDER BY seq", (run_id,)
        )
        return {
            **dict(row),
            "events": [{**dict(event), "data": json.loads(event["data"])} for event in events],
        }

    def abandon(self, run_id: str) -> None:
        """Release a crashed run's session. Never replay tool calls."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT status,pid FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["status"] != "running":
                raise StoreError("run_not_running")
            try:
                os.kill(row["pid"], 0)
            except ProcessLookupError:
                pass
            else:
                raise StoreError("process_still_exists: stop the original process first")
            self.db.execute("UPDATE runs SET status='abandoned' WHERE id=?", (run_id,))
            self.db.execute(
                "INSERT INTO events (run_id,at,name,data) VALUES (?,?,?,?)",
                (run_id, datetime.now(UTC).isoformat(), "run.abandoned", "{}"),
            )
