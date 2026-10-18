"""CLI and server entry points call the same application service."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from learn_agent.application import AgentService, AgentSettings, RecordService, StoreError
from learn_agent.config import load_environment
from learn_agent.contracts import Limits


def agent_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--provider", choices=["dashscope", "openai"], default="dashscope")
    command.add_argument("--model", default=None)
    command.add_argument("--workspace", type=Path, default=Path("examples/workspace"))
    command.add_argument("--allow-write", action="store_true")
    command.add_argument("--max-steps", type=int, default=8)
    command.add_argument("--max-seconds", type=float, default=120)
    command.add_argument("--max-input-bytes", type=int, default=60_000)
    command.add_argument("--max-output-tokens", type=int, default=2048)
    command.add_argument("--verify-file", default=None)
    command.add_argument(
        "--contains", default=None, help="Required text in the first 8000 characters."
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Learn the model → tool → observation loop.")
    root.add_argument("--db", type=Path, default=Path(".learn-agent/state.sqlite3"))
    root.add_argument("--env-file", type=Path, default=Path(".env"))
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run one user turn. Reuse --session for history.")
    run.add_argument("question")
    run.add_argument("--session", default=None)
    run.add_argument("--trace", action="store_true", help="Write metadata events to stderr.")
    run.add_argument("--json", action="store_true", help="Write RunResult JSON to stdout.")
    agent_arguments(run)
    serve = commands.add_parser("serve", help="Open the local Web UI with shared agent services.")
    serve.add_argument("--port", type=int, default=8765)
    agent_arguments(serve)
    inspect = commands.add_parser("inspect", help="Read run metadata and ordered events.")
    inspect.add_argument("run_id")
    abandon = commands.add_parser("abandon", help="Release a crashed run. Never replay effects.")
    abandon.add_argument("run_id")
    return root


def trace(name: str, data: dict[str, Any]) -> None:
    print(json.dumps({"event": name, **data}, ensure_ascii=False), file=sys.stderr)


def main() -> int:
    args = parser().parse_args()
    try:
        load_environment(args.env_file)
        if args.command in ("inspect", "abandon"):
            records = RecordService(args.db)
            if args.command == "inspect":
                print(json.dumps(records.inspect(args.run_id), ensure_ascii=False, indent=2))
            else:
                records.abandon(args.run_id)
                print("Run abandoned. Inspect workspace effects before starting a new run.")
            return 0
        if args.command == "serve" and not 1 <= args.port <= 65535:
            raise ValueError("Port must be between 1 and 65535.")
        service = AgentService(
            AgentSettings(
                db=args.db,
                workspace=args.workspace,
                provider=args.provider,
                model=args.model or "",
                allow_write=args.allow_write,
                verify_file=args.verify_file,
                contains=args.contains,
                limits=Limits(
                    max_steps=args.max_steps,
                    max_seconds=args.max_seconds,
                    max_input_bytes=args.max_input_bytes,
                    max_output_tokens=args.max_output_tokens,
                ),
            )
        )
        if args.command == "serve":
            import uvicorn

            from learn_agent.web import create_app

            print(f"Web UI: http://127.0.0.1:{args.port}", flush=True)
            uvicorn.run(create_app(service), host="127.0.0.1", port=args.port, log_level="info")
            return 0
        result = service.run(
            args.question, session_id=args.session, on_event=trace if args.trace else None
        )
        if args.json:
            print(result.model_dump_json(indent=2))
        else:
            print(f"run={result.run_id} session={result.session_id}")
            print(f"系统结论：{result.outcome.summary}")
            print(result.outcome.detail)
            print(f"status={result.status} verification={result.verification.status}")
            for evidence in result.tool_evidence:
                target = f" {evidence.path}" if evidence.path else ""
                print(f"执行记录：{evidence.tool}{target} → {evidence.code}")
            for check in result.verification.checks:
                print(f"验收检查：{check.target} → {check.status}；{check.detail}")
            if result.answer:
                print("\n模型说明（不作为验收结论）：")
                print(result.answer)
        return 0 if result.status == "completed" else 1
    except (ValueError, OSError, StoreError, ImportError) as exc:
        message = str(exc) if isinstance(exc, (ValueError, StoreError)) else type(exc).__name__
        if isinstance(exc, ImportError):
            message += ": install Web dependencies with uv sync --extra web"
        print(f"error: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
