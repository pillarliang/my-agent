"""Own background tasks. SSE reads durable events, not these in-memory tasks."""

import asyncio
import logging
from typing import Any

from learn_agent.application import AgentService
from learn_agent.store import StoreError

logger = logging.getLogger(__name__)


class CapacityError(Exception):
    pass


class WebRuns:
    def __init__(self, service: AgentService, *, capacity: int = 4) -> None:
        self.service = service
        self.capacity = capacity
        self.tasks: set[asyncio.Task[None]] = set()

    async def submit(self, session_id: str, question: str) -> str:
        session = await asyncio.to_thread(self.service.get_session, session_id)
        if not session["compatible"]:
            raise StoreError("session_binding_mismatch: use a new session")
        if len(self.tasks) >= self.capacity:
            raise CapacityError("server_busy")
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[str] = loop.create_future()
        started_id: str | None = None

        def accept(run_id: str) -> None:
            if not ready.done():
                ready.set_result(run_id)

        def on_event(name: str, data: dict[str, Any]) -> None:
            nonlocal started_id
            if name == "run.started":
                started_id = data["run_id"]
                loop.call_soon_threadsafe(accept, data["run_id"])

        async def execute() -> None:
            try:
                await asyncio.to_thread(
                    self.service.run, question, session_id=session_id, on_event=on_event
                )
            except Exception as exc:
                if started_id is None:
                    if not ready.done():
                        ready.set_exception(exc)
                else:
                    logger.error("Background run failed: %s", type(exc).__name__)
                    try:
                        await asyncio.to_thread(self.service.fail_running, started_id)
                    except Exception as save_error:
                        logger.error("Could not save terminal state: %s", type(save_error).__name__)

        task = asyncio.create_task(execute())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        # A disconnected POST must not cancel an accepted run.
        return await asyncio.shield(ready)

    async def shutdown(self) -> None:
        if self.tasks:
            await asyncio.gather(*list(self.tasks))
