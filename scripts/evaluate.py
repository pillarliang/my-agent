"""Evaluate real models on isolated tasks. Every model call uses the configured API."""

import argparse
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any

from learn_agent.application import AgentService, AgentSettings
from learn_agent.config import Provider, load_environment

ROOT = Path(__file__).resolve().parents[1]


def run_case(case: dict[str, Any], provider: Provider, model_name: str) -> dict[str, Any]:
    with TemporaryDirectory(prefix="learn-agent-eval-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "notes.txt").write_text(
            "项目：Learn Agent\n目标：先理解循环，再增加上下文和记忆。\n验收标记：LEARN-42\n",
            encoding="utf-8",
        )
        service = AgentService(
            AgentSettings(
                db=root / "data/state.sqlite3",
                workspace=workspace,
                provider=provider,
                model=model_name,
                allow_write=case["allow_write"],
                verify_file="report.md" if case["check"] == "artifact" else None,
                contains=case["expected"] if case["check"] == "artifact" else None,
            )
        )
        started = monotonic()
        result = service.run(case["question"], session_id=case["id"])
        events = service.inspect(result.run_id)["events"]
        if case["check"] == "artifact":
            passed = result.status == "completed" and result.verification.status == "passed"
        elif case["check"] == "answer":
            read_succeeded = any(
                event["name"] == "tool.finished"
                and event["data"].get("tool") == "read_file"
                and event["data"].get("ok")
                for event in events
            )
            passed = (
                result.status == "completed"
                and read_succeeded
                and case["expected"] in result.answer
                and "notes.txt" in result.answer
            )
        else:
            passed = result.status == "completed" and not (workspace / "report.md").exists()
        return {
            "case_id": case["id"],
            "check": case["check"],
            "passed": passed,
            "status": result.status,
            "verification": result.verification.status,
            "run_outcome": result.outcome.status,
            "expected": case["expected"],
            "steps": result.steps,
            "seconds": round(monotonic() - started, 3),
            "known_input_tokens": result.input_tokens,
            "known_output_tokens": result.output_tokens,
            "usage_complete": result.usage_complete,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["dashscope", "openai"], default="dashscope")
    parser.add_argument("--model", default="")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    load_environment(args.env_file)
    try:
        settings = AgentSettings(provider=args.provider, model=args.model)
        cases = json.loads((ROOT / "evals/cases.json").read_text(encoding="utf-8"))
        results = [run_case(case, settings.provider, settings.model) for case in cases]
    except (ValueError, OSError) as exc:
        print(
            f"error: {str(exc) if isinstance(exc, ValueError) else type(exc).__name__}",
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "mode": "live_model_eval",
                "provider": settings.provider,
                "model": settings.model,
                "passed": sum(row["passed"] for row in results),
                "total": len(results),
                "cases": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all(row["passed"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
