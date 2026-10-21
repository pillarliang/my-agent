"""独立产物验收：读取真实文件，依据应用规则判断，不采信模型的完成声明。"""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol

from .policy import valid_resource
from .tools import ToolContext, ToolExecutionError, ToolResult
from .tools.filesystem import read_workspace_text


@dataclass(frozen=True)
class VerificationSpec:
    """第一版的三行报告规则；目标、来源与格式由应用配置，不由模型修改。"""

    artifact: Path
    source: Path = Path("notes.txt")
    title: str = "# 验收报告"
    marker_prefix: str = "验收标记："

    def __post_init__(self) -> None:
        for resource in (self.artifact, self.source):
            if not valid_resource(resource):
                raise ValueError(f"验收目标与来源必须是非隐藏的工作区相对文件：{resource}")
        if self.artifact == self.source:
            raise ValueError("产物与来源必须是不同文件。")
        if any(
            not text or text.splitlines() != [text] for text in (self.title, self.marker_prefix)
        ):
            raise ValueError("标题与标记前缀必须是非空单行文本。")


@dataclass(frozen=True)
class VerificationRequest:
    """规则与本次回执一起进入验收；没有规则表示未配置，不能推断通过。"""

    spec: VerificationSpec | None
    tool_results: tuple[ToolResult, ...] = ()


@dataclass(frozen=True)
class CheckResult:
    """一个具体条件的结论及观察证据，便于指出哪一项不满足。"""

    name: str
    passed: bool
    evidence: str


@dataclass(frozen=True)
class VerificationResult:
    """结论只覆盖已配置规则和本次观察；摘要标识被检查的文件内容。"""

    checks: tuple[CheckResult, ...] = ()
    artifact_sha256: str | None = None
    source_sha256: str | None = None

    @property
    def status(self) -> Literal["passed", "failed", "not_checked"]:
        if not self.checks:
            return "not_checked"
        return "passed" if all(check.passed for check in self.checks) else "failed"


class Verifier(Protocol):
    """应用依赖的黑盒接口；以后可换检查器，无需在 Runner 中添加业务判断。"""

    def verify(self, request: VerificationRequest) -> VerificationResult: ...


class ReportVerifier:
    """检查本次新建报告；复用有界文件读取规则，不通过模型或工具分发取证。"""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    def verify(self, request: VerificationRequest) -> VerificationResult:
        """失败保留具体检查证据；缺少前置文件时停止依赖它的检查，不猜测内容。"""
        spec = request.spec
        if spec is None:
            return VerificationResult()
        creations = [
            result
            for result in request.tool_results
            if result.name == "create_file"
            and result.content["ok"]
            and result.content["data"]["path"] == spec.artifact.as_posix()
        ]
        receipt = creations[-1] if creations else None
        checks = [
            CheckResult(
                "created_this_run",
                receipt is not None,
                f"创建回执：{receipt.call_id}" if receipt else "本次没有该路径的成功创建回执。",
            )
        ]
        try:
            text = read_workspace_text(spec.artifact.as_posix(), self._context)
        except ToolExecutionError as exc:
            checks.append(CheckResult("artifact_readable", False, f"{exc.code}: {exc}"))
            return VerificationResult(tuple(checks))
        digest = sha256(text.encode("utf-8")).hexdigest()
        checks.append(CheckResult("artifact_readable", True, f"{spec.artifact} SHA-256={digest}"))
        if receipt is not None and receipt.content["ok"]:
            expected_digest = receipt.content["data"]["sha256"]
            checks.append(
                CheckResult(
                    "artifact_matches_receipt",
                    digest == expected_digest,
                    f"创建摘要={expected_digest}；实际摘要={digest}",
                )
            )
        lines = text.splitlines()
        checks.append(
            CheckResult(
                "report_format",
                len(lines) == 3
                and lines[0] == spec.title
                and lines[1].startswith(spec.marker_prefix)
                and lines[2].startswith("来源："),
                f"要求三行且标题为 {spec.title!r}；实际 {len(lines)} 行，"
                f"首行为 {lines[0] if lines else None!r}。",
            )
        )
        try:
            source = read_workspace_text(spec.source.as_posix(), self._context)
        except ToolExecutionError as exc:
            checks.append(CheckResult("source_readable", False, f"{exc.code}: {exc}"))
            return VerificationResult(tuple(checks), digest)
        source_digest = sha256(source.encode("utf-8")).hexdigest()
        checks.append(
            CheckResult("source_readable", True, f"{spec.source} SHA-256={source_digest}")
        )
        markers = [
            (number, line)
            for number, line in enumerate(source.splitlines(), start=1)
            if line.startswith(spec.marker_prefix) and line.removeprefix(spec.marker_prefix).strip()
        ]
        checks.append(
            CheckResult(
                "source_marker", len(markers) == 1, f"来源中发现 {len(markers)} 个非空标记。"
            )
        )
        if len(markers) == 1:
            number, marker = markers[0]
            reference = f"来源：{spec.source.as_posix()}:{number}"
            actual_marker = lines[1] if len(lines) >= 2 else None
            actual_reference = lines[2] if len(lines) >= 3 else None
            checks.extend(
                [
                    CheckResult(
                        "marker_matches_source",
                        actual_marker == marker,
                        f"{spec.source}:{number} 原文={marker!r}；报告第二行={actual_marker!r}",
                    ),
                    CheckResult(
                        "source_reference",
                        actual_reference == reference,
                        f"期望第三行={reference!r}；实际={actual_reference!r}",
                    ),
                ]
            )
        return VerificationResult(tuple(checks), digest, source_digest)
