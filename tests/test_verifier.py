"""用真实文件验收产物；本机 SDK 响应只验证执行与验收的黑盒连接。"""

import json
from hashlib import sha256
from pathlib import Path

import pytest
from protocol_support import completion, local_api

from learn_agent.models import OpenAIChatModel
from learn_agent.policy import Policy, PolicyService
from learn_agent.runner import run_agent
from learn_agent.tools import CREATE_FILE, ToolCall, ToolContext, ToolRegistry
from learn_agent.verifier import ReportVerifier, VerificationRequest, VerificationSpec

REPORT = "# 验收报告\n验收标记：LEARN-42\n来源：notes.txt:3\n"


@pytest.fixture
def environment(tmp_path):
    (tmp_path / "notes.txt").write_text(
        "项目：Learn Agent\n说明\n验收标记：LEARN-42\n", encoding="utf-8"
    )
    context = ToolContext(
        tmp_path, PolicyService(Policy(create_files=frozenset({Path("report.md")})))
    )
    return ToolRegistry([CREATE_FILE], context), ReportVerifier(context)


def create(tools, content=REPORT):
    return tools.execute(
        ToolCall(
            "creation_actual", "create_file", json.dumps({"path": "report.md", "content": content})
        )
    )


def verify(verifier, *results):
    return verifier.verify(VerificationRequest(VerificationSpec(Path("report.md")), results))


def checks_by_name(result):
    return {check.name: check for check in result.checks}


def test_verification_without_spec_is_not_checked_and_does_not_read_files(tmp_path):
    result = ReportVerifier(ToolContext(tmp_path / "missing")).verify(VerificationRequest(None))
    assert result.status == "not_checked"
    assert result.checks == ()
    assert result.artifact_sha256 is None


def test_created_report_passes_with_real_content_hashes_and_source_line(environment, tmp_path):
    tools, verifier = environment
    receipt = create(tools)
    result = verify(verifier, receipt)
    assert result.status == "passed"
    assert result.artifact_sha256 == sha256((tmp_path / "report.md").read_bytes()).hexdigest()
    assert result.source_sha256 == sha256((tmp_path / "notes.txt").read_bytes()).hexdigest()
    checks = checks_by_name(result)
    assert checks["created_this_run"].evidence == "创建回执：creation_actual"
    assert "notes.txt:3" in checks["source_reference"].evidence
    assert all(check.passed for check in checks.values())


@pytest.mark.parametrize(
    "content,failed_check",
    [
        (REPORT.replace("LEARN-42", "WRONG"), "marker_matches_source"),
        (REPORT.replace("来源：notes.txt:3\n", ""), "source_reference"),
        (REPORT.replace("notes.txt:3", "notes.txt:2"), "source_reference"),
        (REPORT.replace("notes.txt:3", "other.txt:3"), "source_reference"),
        (REPORT.replace("# 验收报告", "任务已完成"), "report_format"),
        ("```text\n" + REPORT + "```\n", "report_format"),
        (REPORT + "额外说明\n", "report_format"),
        ("", "report_format"),
    ],
)
def test_successful_creation_does_not_imply_acceptance(environment, content, failed_check):
    tools, verifier = environment
    receipt = create(tools, content)
    assert receipt.content["ok"] is True
    result = verify(verifier, receipt)
    assert result.status == "failed"
    checks = checks_by_name(result)
    assert checks["created_this_run"].passed
    assert checks["artifact_matches_receipt"].passed
    assert not checks[failed_check].passed


def test_old_correct_report_cannot_count_as_this_run_creation(environment, tmp_path):
    tools, verifier = environment
    (tmp_path / "report.md").write_text(REPORT, encoding="utf-8")
    failed_creation = create(tools)
    assert failed_creation.content["error"]["code"] == "file_exists"
    result = verify(verifier, failed_creation)
    assert result.status == "failed"
    checks = checks_by_name(result)
    assert not checks["created_this_run"].passed
    assert checks["marker_matches_source"].passed
    assert (tmp_path / "report.md").read_text() == REPORT


def test_receipts_from_other_targets_cannot_supply_creation_proof(environment):
    tools, verifier = environment
    receipt = create(tools)
    result = verifier.verify(VerificationRequest(VerificationSpec(Path("other.md")), (receipt,)))
    assert result.status == "failed"
    assert not checks_by_name(result)["created_this_run"].passed


def test_content_changed_after_creation_is_detected_even_if_new_content_meets_rules(
    environment, tmp_path
):
    tools, verifier = environment
    receipt = create(tools)
    # 三行语义仍然正确，但字节由 LF 改为 CRLF，必须识别这不是回执中的版本。
    (tmp_path / "report.md").write_bytes(REPORT.replace("\n", "\r\n").encode("utf-8"))
    result = verify(verifier, receipt)
    checks = checks_by_name(result)
    assert result.status == "failed"
    assert checks["report_format"].passed
    assert checks["marker_matches_source"].passed
    assert not checks["artifact_matches_receipt"].passed


@pytest.mark.parametrize(
    "change", ["deleted", "directory", "binary", "oversized", "outside_symlink"]
)
def test_missing_or_unreadable_artifact_cannot_pass(environment, tmp_path, change):
    tools, verifier = environment
    receipt = create(tools)
    target = tmp_path / "report.md"
    target.unlink()
    if change == "directory":
        target.mkdir()
    elif change == "binary":
        target.write_bytes(b"\xff")
    elif change == "oversized":
        target.write_bytes(b"x" * 16_001)
    elif change == "outside_symlink":
        target.symlink_to(tmp_path.parent / "outside.md")
    result = verify(verifier, receipt)
    assert result.status == "failed"
    assert not checks_by_name(result)["artifact_readable"].passed


@pytest.mark.parametrize("source", [None, "没有标记", "验收标记：\n", "验收标记：A\n验收标记：B\n"])
def test_missing_empty_or_ambiguous_source_marker_does_not_get_guessed(
    environment, tmp_path, source
):
    tools, verifier = environment
    receipt = create(tools)
    if source is None:
        (tmp_path / "notes.txt").unlink()
    else:
        (tmp_path / "notes.txt").write_text(source, encoding="utf-8")
    result = verify(verifier, receipt)
    assert result.status == "failed"
    assert not checks_by_name(result)[
        "source_readable" if source is None else "source_marker"
    ].passed


def test_expected_source_is_read_from_configured_file_not_hardcoded_example(environment, tmp_path):
    tools, verifier = environment
    (tmp_path / "facts.txt").write_text("验收标记：DIFFERENT\n", encoding="utf-8")
    receipt = create(tools, "# 自定义标题\n验收标记：DIFFERENT\n来源：facts.txt:1\n")
    spec = VerificationSpec(Path("report.md"), source=Path("facts.txt"), title="# 自定义标题")
    assert verifier.verify(VerificationRequest(spec, (receipt,))).status == "passed"


def test_changed_source_is_detected_by_fresh_read(environment, tmp_path):
    tools, verifier = environment
    receipt = create(tools)
    (tmp_path / "notes.txt").write_text("验收标记：NEW\n", encoding="utf-8")
    result = verify(verifier, receipt)
    assert result.status == "failed"
    assert not checks_by_name(result)["marker_matches_source"].passed
    assert not checks_by_name(result)["source_reference"].passed


@pytest.mark.parametrize(
    "kind",
    [
        "artifact",
        "source",
        "title",
        "marker_prefix",
        "same_file",
        "title_newline",
        "prefix_newline",
    ],
)
def test_invalid_spec_is_a_configuration_error(kind):
    options = {"artifact": Path("report.md")}
    if kind in ("artifact", "source"):
        options[kind] = Path("../outside.txt")
    elif kind == "same_file":
        options["source"] = Path("report.md")
    elif kind == "title_newline":
        options["title"] = "标题\n"
    elif kind == "prefix_newline":
        options["marker_prefix"] = "\n"
    else:
        options[kind] = ""
    with pytest.raises(ValueError):
        VerificationSpec(**options)


def test_programming_errors_in_verifier_are_not_converted_to_failed_checks(
    environment, monkeypatch
):
    tools, verifier = environment
    receipt = create(tools)

    def broken_reader(*args):
        raise TypeError("验收器程序错误")

    monkeypatch.setattr("learn_agent.verifier.read_workspace_text", broken_reader)
    with pytest.raises(TypeError, match="验收器程序错误"):
        verify(verifier, receipt)


def test_runner_and_verifier_are_black_boxes_connected_by_application(environment):
    tools, verifier = environment
    with local_api(
        [
            completion(
                name="create_file",
                arguments={"path": "report.md", "content": REPORT},
                finish="tool_calls",
            ),
            completion(text="我说失败了"),
        ]
    ) as (client, requests):
        run = run_agent(OpenAIChatModel(client, "local"), "生成报告", tools)
    assert run.answer == "我说失败了"
    assert len(run.tool_results) == 1
    assert run.tool_results[0].call_id == "call_0"
    assert json.loads(requests[1]["messages"][-1]["content"])["data"]["sha256"]
    assert verify(verifier, *run.tool_results).status == "passed"


@pytest.mark.parametrize(
    "content,exit_code", [(REPORT, 0), (REPORT.replace("来源：notes.txt:3\n", ""), 1)]
)
def test_cli_displays_independent_verdict_and_returns_failure_exit_code(
    tmp_path, monkeypatch, capsys, content, exit_code
):
    from learn_agent import __main__ as entry

    (tmp_path / "notes.txt").write_text("项目\n说明\n验收标记：LEARN-42\n", encoding="utf-8")
    with local_api(
        [
            completion(
                name="create_file",
                arguments={"path": "report.md", "content": content},
                finish="tool_calls",
            ),
            completion(text="报告已完美完成，验收通过！"),
        ]
    ) as (client, requests):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "local-protocol-test")
        monkeypatch.setenv("DASHSCOPE_BASE_URL", str(client.base_url))
        monkeypatch.setenv("DASHSCOPE_MODEL", "local")
        monkeypatch.setenv("DASHSCOPE_USE_ENV_PROXY", "false")
        assert entry.main(["--workspace", str(tmp_path), "--create-file", "report.md"]) == exit_code
    output = capsys.readouterr().out
    assert "报告已完美完成" in output
    assert f"产物验收：{'passed' if exit_code == 0 else 'failed'}" in output
    assert len(requests) == 2
