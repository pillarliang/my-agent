import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers import REPORT_TASK


@pytest.mark.live
def test_cli_end_to_end_and_inspection(live_settings):
    prefix = [sys.executable, "-m", "learn_agent.cli", "--db", str(live_settings.db)]
    completed = subprocess.run(
        [
            *prefix,
            "run",
            REPORT_TASK,
            "--workspace",
            str(live_settings.workspace),
            "--allow-write",
            "--verify-file",
            "report.md",
            "--contains",
            "LEARN-42",
            "--trace",
            "--json",
            "--max-steps",
            "5",
            "--max-output-tokens",
            "512",
            "--max-seconds",
            "60",
        ],
        text=True,
        capture_output=True,
        check=True,
        timeout=75,
    )
    result = json.loads(completed.stdout)
    assert result["status"] == "completed" and result["verification"]["status"] == "passed"
    assert result["outcome"]["status"] == "passed"
    assert any(item["tool"] == "create_file" and item["ok"] for item in result["tool_evidence"])
    assert "tool.finished" in completed.stderr
    inspected = subprocess.run(
        [*prefix, "inspect", result["run_id"]],
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(inspected.stdout)["events"][-1]["name"] == "run.finished"


def test_cli_invalid_limits_do_not_create_run(tmp_path: Path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "learn_agent.cli",
            "--db",
            str(tmp_path / "db.sqlite3"),
            "run",
            "x",
            "--workspace",
            str(tmp_path),
            "--max-seconds",
            "nan",
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2 and "finite" in result.stderr
    assert not (tmp_path / "db.sqlite3").exists()


def test_cli_checks_verifier_configuration(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, "-m", "learn_agent.cli", "run", "x", "--contains", "x"],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2 and "together" in result.stderr


def test_cli_missing_key_fails_without_fallback(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "learn_agent.cli",
            "--env-file",
            str(tmp_path / "absent.env"),
            "run",
            "x",
            "--workspace",
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2 and "DASHSCOPE_API_KEY" in result.stderr


@pytest.mark.live
def test_cli_unverified_output_separates_model_explanation(live_settings):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "learn_agent.cli",
            "--db",
            str(live_settings.db),
            "run",
            "用一句话解释 Agent Loop，不要创建文件。",
            "--workspace",
            str(live_settings.workspace),
            "--max-output-tokens",
            "256",
        ],
        text=True,
        capture_output=True,
        check=True,
        timeout=45,
    )
    assert "系统结论：结果未验证" in result.stdout
    assert "模型说明（不作为验收结论）" in result.stdout
    assert result.stdout.index("系统结论") < result.stdout.index("模型说明")
