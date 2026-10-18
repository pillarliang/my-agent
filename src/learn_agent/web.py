"""Local HTTP adapter. It translates requests and never implements an agent loop."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from learn_agent.application import AgentService
from learn_agent.store import StoreError
from learn_agent.web_runtime import CapacityError, WebRuns

STATIC = Path(__file__).with_name("webui")


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, max_length=16_000)


def create_app(service: AgentService) -> FastAPI:
    runs = WebRuns(service)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await runs.shutdown()

    app = FastAPI(
        title="Learn Agent", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.state.runs = runs
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def local_boundary(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path.startswith("/api/") and request.method == "POST":
            origin = request.headers.get("origin")
            if request.headers.get("x-learn-agent") != "web" or (
                origin is not None
                and (
                    urlsplit(origin).scheme != "http"
                    or urlsplit(origin).netloc != request.url.netloc
                )
            ):
                return JSONResponse({"detail": "same_origin_required"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.exception_handler(StoreError)
    async def store_error(request: Request, exc: StoreError) -> JSONResponse:
        code = str(exc).split(":", 1)[0]
        status = 404 if code.endswith("not_found") else 409
        return JSONResponse({"detail": code}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "invalid_request"}, status_code=422)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return service.configuration()

    @app.get("/api/sessions")
    def sessions() -> dict[str, Any]:
        return {"sessions": service.list_sessions()}

    @app.post("/api/sessions", status_code=201)
    def create_session() -> dict[str, Any]:
        return service.create_session()

    @app.get("/api/sessions/{session_id}")
    def history(
        session_id: str, before: Annotated[int | None, Query(ge=1)] = None
    ) -> dict[str, Any]:
        return service.get_session(session_id, before=before)

    @app.post("/api/sessions/{session_id}/runs", status_code=202)
    async def start(session_id: str, body: ChatRequest) -> dict[str, str]:
        if not body.question.strip():
            raise HTTPException(422, "empty_question")
        try:
            run_id = await runs.submit(session_id, body.question)
        except CapacityError as exc:
            raise HTTPException(429, "server_busy") from exc
        except (ValueError, ImportError) as exc:
            raise HTTPException(503, "model_configuration_error") from exc
        return {"run_id": run_id, "session_id": session_id}

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict[str, Any]:
        return service.get_run(run_id)

    @app.get("/api/runs/{run_id}/events")
    async def events(
        run_id: str, request: Request, after: Annotated[int, Query(ge=0)] = 0
    ) -> StreamingResponse:
        cursor = after
        if "last-event-id" in request.headers:
            try:
                cursor = max(cursor, int(request.headers["last-event-id"]))
            except ValueError as exc:
                raise HTTPException(400, "invalid_event_cursor") from exc
        first = await asyncio.to_thread(service.events, run_id, cursor)

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor
            page = first
            ticks = 0
            while True:
                if await request.is_disconnected():
                    return
                for event in page["events"]:
                    cursor = event["seq"]
                    data = json.dumps(event, ensure_ascii=False)
                    yield f"id: {cursor}\nevent: progress\ndata: {data}\n\n"
                if page["status"] != "running" and len(page["events"]) < 200:
                    yield "event: end\ndata: " + json.dumps({"status": page["status"]}) + "\n\n"
                    return
                ticks += 1
                if ticks % 40 == 0:
                    yield ": keepalive\n\n"
                if len(page["events"]) < 200:
                    await asyncio.sleep(0.25)
                page = await asyncio.to_thread(service.events, run_id, cursor)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    return app
