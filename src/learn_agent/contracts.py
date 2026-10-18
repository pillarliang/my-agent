"""Shared values and ports. This module imports no concrete adapters."""

from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCall(Value):
    id: str
    name: str
    arguments: str  # Keep the original JSON. The executor validates it.


class ToolResult(Value):
    ok: bool
    code: str
    data: dict[str, Any] = Field(default_factory=dict)


class Message(Value):
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    calls: list[ToolCall] = Field(default_factory=list)
    call_id: str = ""
    result: ToolResult | None = None
    # Opaque continuation data belongs to the provider, not to loop decisions.
    provider_items: list[dict[str, Any]] = Field(default_factory=list)


class Reply(Value):
    text: str = ""
    calls: list[ToolCall] = Field(default_factory=list)
    provider_items: list[dict[str, Any]] = Field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class Context:
    instructions: str
    messages: list[Message]
    tools: list[ToolSpec]
    omitted_turns: int = 0
    input_bytes: int = 0  # A size guard, NOT a tokenizer measurement.


class Model(Protocol):
    def respond(self, context: Context, *, timeout: float, max_output_tokens: int) -> Reply: ...


class ModelError(Exception):
    """Public code only. Raw SDK exceptions can contain sensitive data."""


class ContextOverflow(Exception):
    pass


Status = Literal[
    "completed",
    "verification_failed",
    "step_limit",
    "time_limit",
    "context_limit",
    "model_error",
    "protocol_error",
    "cancelled",
    "internal_error",
]


class CheckResult(Value):
    name: str
    status: Literal["passed", "failed"]
    target: str
    detail: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class Verification(Value):
    status: Literal["unchecked", "passed", "failed"] = "unchecked"
    detail: str = "No task verifier was configured."
    checks: list[CheckResult] = Field(default_factory=list)


class ToolEvidence(Value):
    call_id: str
    tool: str
    ok: bool
    code: str
    path: str | None = None
    sha256: str | None = None


class Outcome(Value):
    status: Literal["passed", "failed", "unverified", "incomplete"] = "unverified"
    summary: str = "结果未验证"
    detail: str = "尚无任务验收结论。"


class Verifier(Protocol):
    def check(self, answer: str) -> Verification: ...


class Store(Protocol):
    def start(self, session_id: str, workspace: str, model_key: str) -> str: ...
    def history(self, session_id: str) -> list[list[Message]]: ...
    def save_messages(self, run_id: str, messages: list[Message]) -> None: ...
    def event(self, run_id: str, name: str, data: dict[str, Any]) -> None: ...
    def finish(self, result: "RunResult", messages: list[Message]) -> None: ...


class RunResult(Value):
    run_id: str
    session_id: str
    status: Status
    answer: str = ""  # Model explanation only; the program's verdict is outcome.
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_complete: bool = True
    verification: Verification = Field(default_factory=Verification)
    tool_evidence: list[ToolEvidence] = Field(default_factory=list)
    outcome: Outcome = Field(default_factory=Outcome)

    @model_validator(mode="after")
    def derive_outcome(self) -> Self:
        # This conclusion comes from program state, never from the model's answer.
        if self.status == "verification_failed" or self.verification.status == "failed":
            outcome = Outcome(
                status="failed",
                summary="验收检查未通过",
                detail="指定条件未满足，不能按任务已完成交付。",
            )
        elif self.status != "completed":
            outcome = Outcome(
                status="incomplete",
                summary="执行未完成",
                detail=f"运行停止于 {self.status}；已发生的工具操作仍需核对。",
            )
        elif self.verification.status == "passed":
            outcome = Outcome(
                status="passed",
                summary="已通过指定检查",
                detail="仅确认下列检查条件满足，不代表所有需求均已验收。"
                if self.verification.checks
                else "历史记录显示检查通过，但未保存检查范围明细。",
            )
        else:
            outcome = Outcome(
                status="unverified",
                summary="结果未验证",
                detail="模型已返回说明，但未配置验收检查，无法确认任务已完成。",
            )
        object.__setattr__(self, "outcome", outcome)
        return self


@dataclass(frozen=True)
class Limits:
    max_steps: int = 8
    max_seconds: float = 120
    model_timeout: float = 30
    max_output_tokens: int = 2048
    max_input_bytes: int = 60_000
    max_calls_per_step: int = 8

    def __post_init__(self) -> None:
        if any(not isfinite(value) or value <= 0 for value in vars(self).values()):
            raise ValueError("All limits must be finite and positive.")


@dataclass
class RunState:
    """Mutable state owned by exactly one Runner invocation."""

    current: list[Message]
    seen_calls: set[str] = field(default_factory=set)
