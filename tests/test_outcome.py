import hashlib
import json

import pytest

from learn_agent.contracts import Outcome, RunResult, Verification
from learn_agent.policy import Workspace
from learn_agent.verification import FileContainsVerifier


@pytest.mark.parametrize(
    ("status", "verification", "outcome"),
    [
        ("completed", "unchecked", "unverified"),
        ("completed", "passed", "passed"),
        ("verification_failed", "failed", "failed"),
        ("model_error", "unchecked", "incomplete"),
        ("time_limit", "passed", "incomplete"),
    ],
)
def test_system_outcome_follows_runtime_and_checks(status, verification, outcome):
    result = RunResult(
        run_id="r",
        session_id="s",
        status=status,
        verification=Verification(status=verification),
    )
    assert result.outcome.status == outcome


def test_serialized_outcome_is_recomputed_from_program_state():
    result = RunResult(run_id="r", session_id="s", status="completed")
    data = result.model_dump()
    data["outcome"] = Outcome(status="passed", summary="passed").model_dump()
    restored = RunResult.model_validate(data)
    assert restored.outcome.status == "unverified"
    assert RunResult.model_validate_json(restored.model_dump_json()) == restored


def test_existing_records_get_explicit_unverified_conclusion(store, tmp_path):
    run_id = store.start("s", str(tmp_path), "dashscope:history")
    store.finish(RunResult(run_id=run_id, session_id="s", status="completed"), [])
    legacy = json.loads(
        store.db.execute("SELECT result FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    )
    legacy.pop("outcome")
    legacy.pop("tool_evidence")
    legacy["verification"].pop("checks")
    with store.db:
        store.db.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps(legacy), run_id))
    public = store.run_detail(run_id)["result"]
    assert public["outcome"]["status"] == "unverified"
    assert public["tool_evidence"] == []


def test_verification_reports_actual_scope_and_file_hash(tmp_path):
    content = "验收标记：LEARN-42"
    (tmp_path / "report.md").write_text(content, encoding="utf-8")
    verification = FileContainsVerifier(Workspace(tmp_path), "report.md", "LEARN-42").check("")
    check = verification.checks[0]
    assert verification.status == check.status == "passed"
    assert check.target == "report.md" and check.name == "file_contains"
    assert check.evidence["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert check.evidence["checked_chars"] == len(content)


def test_failed_check_records_file_read_failure(tmp_path):
    verification = FileContainsVerifier(Workspace(tmp_path), "missing.md", "marker").check("")
    assert verification.status == "failed"
    assert verification.checks[0].evidence["read_code"] == "not_found"
