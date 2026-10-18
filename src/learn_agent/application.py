"""Shared use cases for CLI and HTTP. No HTTP types or browser state live here."""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from learn_agent.config import Provider, dashscope_endpoint, dashscope_use_env_proxy, resolve_model
from learn_agent.contracts import Limits, Model, RunResult
from learn_agent.policy import Workspace
from learn_agent.runner import Runner
from learn_agent.store import SQLiteStore
from learn_agent.store import StoreError as StoreError
from learn_agent.tools import ToolExecutor
from learn_agent.verification import FileContainsVerifier

EventCallback = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class AgentSettings:
    db: Path = Path(".learn-agent/state.sqlite3")
    workspace: Path = Path("examples/workspace")
    provider: Provider = "dashscope"
    model: str = ""
    allow_write: bool = False
    limits: Limits = field(default_factory=Limits)
    verify_file: str | None = None
    contains: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", resolve_model(self.provider, self.model or None))


class RecordService:
    """Record-only use cases need no model or workspace configuration."""

    def __init__(self, db: Path) -> None:
        self.db = db
        SQLiteStore(db).close()

    def _store(self) -> SQLiteStore:
        return SQLiteStore(self.db, initialize=False)

    def inspect(self, run_id: str) -> dict[str, Any]:
        store = self._store()
        try:
            return store.inspect(run_id)
        finally:
            store.close()

    def abandon(self, run_id: str) -> None:
        store = self._store()
        try:
            store.abandon(run_id)
        finally:
            store.close()


class AgentService(RecordService):
    def __init__(self, settings: AgentSettings) -> None:
        if (settings.verify_file is None) != (settings.contains is None):
            raise ValueError("Use --verify-file and --contains together.")
        if settings.contains == "":
            raise ValueError("--contains must not be empty.")
        key_name = "DASHSCOPE_API_KEY" if settings.provider == "dashscope" else "OPENAI_API_KEY"
        if not os.environ.get(key_name):
            raise ValueError(f"Set {key_name} before calling the model.")
        self.settings = settings
        self.workspace = Workspace(settings.workspace, allow_write=settings.allow_write)
        self.model_key = f"{settings.provider}:{settings.model}"
        if settings.provider == "dashscope":
            self._dashscope_endpoint = dashscope_endpoint()
            self._dashscope_use_env_proxy = dashscope_use_env_proxy()
            endpoint_id = sha256(self._dashscope_endpoint.encode()).hexdigest()[:16]
            self.model_key += f"@{endpoint_id}"
        # Initialize once. Each use case opens its own thread-owned connection.
        super().__init__(settings.db)

    def _default_model(self) -> Model:
        if self.settings.provider == "dashscope":
            from learn_agent.providers.dashscope import DashScopeModel

            return DashScopeModel(
                self.settings.model,
                api_key=os.environ["DASHSCOPE_API_KEY"],
                base_url=self._dashscope_endpoint,
                use_env_proxy=self._dashscope_use_env_proxy,
            )
        from learn_agent.providers.openai import OpenAIModel

        return OpenAIModel(self.settings.model)

    def configuration(self) -> dict[str, Any]:
        return {
            "provider": self.settings.provider,
            "model": self.settings.model,
            "workspace": str(self.workspace.root),
            "allow_write": self.settings.allow_write,
            "max_steps": self.settings.limits.max_steps,
            "verification": self.settings.verify_file is not None,
        }

    def run(
        self, question: str, *, session_id: str | None = None, on_event: EventCallback | None = None
    ) -> RunResult:
        model = self._default_model()
        store = self._store()
        try:
            verifier = (
                FileContainsVerifier(
                    self.workspace, self.settings.verify_file, self.settings.contains or ""
                )
                if self.settings.verify_file is not None
                else None
            )
            return Runner(
                model,
                ToolExecutor(self.workspace),
                store,
                model_key=self.model_key,
                limits=self.settings.limits,
                verifier=verifier,
                on_event=on_event,
            ).run(question, session_id=session_id or uuid4().hex)
        finally:
            store.close()
            close = getattr(model, "close", None)
            if close is not None:
                close()

    def create_session(self) -> dict[str, Any]:
        session_id = uuid4().hex
        store = self._store()
        try:
            store.create_session(session_id, str(self.workspace.root), self.model_key)
            return {"id": session_id}
        finally:
            store.close()

    def list_sessions(self) -> list[dict[str, Any]]:
        store = self._store()
        try:
            sessions = store.sessions()
            for session in sessions:
                session["compatible"] = (
                    session["workspace"] == str(self.workspace.root)
                    and session["model_key"] == self.model_key
                )
            return sessions
        finally:
            store.close()

    def get_session(self, session_id: str, *, before: int | None = None) -> dict[str, Any]:
        store = self._store()
        try:
            result = store.session(session_id, before=before)
            result["compatible"] = (
                result["workspace"] == str(self.workspace.root)
                and result["model_key"] == self.model_key
            )
            return result
        finally:
            store.close()

    def get_run(self, run_id: str) -> dict[str, Any]:
        store = self._store()
        try:
            return store.run_detail(run_id)
        finally:
            store.close()

    def events(self, run_id: str, after: int) -> dict[str, Any]:
        store = self._store()
        try:
            return store.events_since(run_id, after)
        finally:
            store.close()

    def fail_running(self, run_id: str) -> None:
        store = self._store()
        try:
            store.fail_running(run_id)
        finally:
            store.close()
