"""阶段 4：在输入预算内选择完整历史任务，只生成请求视图，不改原始记录。"""

from collections.abc import Callable
from dataclasses import dataclass

from .memory import ProjectMemory
from .models import ModelMessage, ModelRequest, ModelResponse, TextMessage
from .models.openai_chat import estimate_input_tokens
from .tools import ToolCall, ToolResult, ToolSpec


def _validate_completed(messages: tuple[ModelMessage, ...]) -> None:
    """completed 历史必须是用户问题、完整调用回执链与最终回答。"""
    if (
        not messages
        or not isinstance(messages[0], TextMessage)
        or messages[0].role != "user"
        or not messages[0].content.strip()
    ):
        raise ValueError("已完成任务缺少用户问题。")
    pending: list[ToolCall] = []
    final = False
    for message in messages[1:]:
        if final:
            raise ValueError("最终回答之后还有消息。")
        if isinstance(message, ModelResponse):
            if pending:
                raise ValueError("已完成任务缺少工具回执。")
            if message.refusal or message.stop_reason not in ("stop", "tool_calls"):
                raise ValueError("已完成任务包含未正常结束的模型响应。")
            pending = list(message.tool_calls)
            if len({call.call_id for call in pending}) != len(pending):
                raise ValueError("同一模型响应中有重复的工具调用 ID。")
            if not pending:
                if not (message.text or "").strip():
                    raise ValueError("已完成任务的最终回答为空。")
                final = True
        elif isinstance(message, ToolResult):
            if not pending or (message.call_id, message.name) != (
                pending[0].call_id,
                pending[0].name,
            ):
                raise ValueError("工具回执与调用不匹配。")
            pending.pop(0)
        else:
            raise ValueError("一个用户任务中不能插入另一个用户问题。")
    if pending or not final:
        raise ValueError("已完成任务的消息链不完整。")


@dataclass(frozen=True)
class HistoryTurn:
    """已完成的用户任务；文件读取与直接传入内存历史共用同一完整性检查。"""

    turn_id: str
    messages: tuple[ModelMessage, ...]

    def __post_init__(self) -> None:
        if not self.turn_id:
            raise ValueError("历史任务 ID 不能为空。")
        _validate_completed(self.messages)


@dataclass(frozen=True)
class ContextBudget:
    """应用配置的窗口额度，不自动推断供应商限制；默认窗口额度为 32K。"""

    context_window: int = 32768
    max_output_tokens: int = 512
    safety_margin: int = 1024
    max_input_tokens: int | None = None

    def __post_init__(self) -> None:
        for name in ("context_window", "max_output_tokens", "safety_margin", "max_input_tokens"):
            value = getattr(self, name)
            if name == "max_input_tokens" and value is None:
                continue
            minimum = 0 if name == "safety_margin" else 1
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} 必须是大于等于 {minimum} 的整数。")
        if self.input_budget <= 0:
            raise ValueError("扣除输出预留与安全余量后的输入预算必须为正数。")

    @property
    def input_budget(self) -> int:
        available = self.context_window - self.max_output_tokens - self.safety_margin
        return (
            min(available, self.max_input_tokens)
            if self.max_input_tokens is not None
            else available
        )


@dataclass(frozen=True)
class InputMeter:
    """计量器与口径名称一起传入；测试或其他协议可替换，计量失败直接传播。"""

    count: Callable[[ModelRequest], int] = estimate_input_tokens
    unit: str = "utf8_json_bytes_estimate"

    def measure(self, request: ModelRequest) -> int:
        used = self.count(request)
        if type(used) is not int or used <= 0:
            raise ValueError("输入计量必须返回正整数，不能把未知体积当成零。")
        return used


DEFAULT_BUDGET = ContextBudget()
DEFAULT_METER = InputMeter()


@dataclass(frozen=True)
class ContextDecision:
    """一次请求的选择依据；历史 ID 按原顺序列出，不包含当前正在执行的任务。"""

    budget: int
    used: int
    unit: str
    kept: tuple[str, ...]
    dropped: tuple[str, ...]


@dataclass(frozen=True)
class ContextSelection:
    request: ModelRequest
    decision: ContextDecision


class ContextOverflowError(RuntimeError):
    """全部可选历史移除后仍超限；保留最终计量与选择明细供调用方检查。"""

    code = "context_overflow"

    def __init__(self, decision: ContextDecision) -> None:
        self.decision = decision
        super().__init__(
            f"必需上下文超出输入预算：used={decision.used}, budget={decision.budget}, "
            f"unit={decision.unit}, kept={decision.kept}, dropped={decision.dropped}。"
        )


def select_context(
    *,
    system: TextMessage,
    history: tuple[HistoryTurn, ...],
    current: tuple[ModelMessage, ...],
    tools: tuple[ToolSpec, ...] = (),
    required_turn_ids: frozenset[str] = frozenset(),
    budget: ContextBudget = DEFAULT_BUDGET,
    meter: InputMeter = DEFAULT_METER,
    memory: ProjectMemory | None = None,
) -> ContextSelection:
    """记忆作为必需参考资料；从最旧的可选任务开始整块移除并重新计量。"""
    ids = [turn.turn_id for turn in history]
    if len(set(ids)) != len(ids):
        raise ValueError("历史任务 ID 不能重复。")
    missing = required_turn_ids.difference(ids)
    if missing:
        raise ValueError(f"必需历史任务不存在或尚未完成：{sorted(missing)}")
    kept = list(history)
    dropped: list[str] = []
    references = (
        (
            TextMessage(
                "user",
                f"【项目参考资料】\n来源文件: {memory.source}\n内容:\n{memory.content}",
            ),
        )
        if memory is not None
        else ()
    )
    while True:
        messages = (
            system,
            *(message for turn in kept for message in turn.messages),
            *references,
            *current,
        )
        request = ModelRequest(messages, tools, budget.max_output_tokens)
        used = meter.measure(request)
        decision = ContextDecision(
            budget.input_budget,
            used,
            meter.unit,
            tuple(turn.turn_id for turn in kept),
            tuple(dropped),
        )
        if used <= decision.budget:
            return ContextSelection(request, decision)
        removable = next((turn for turn in kept if turn.turn_id not in required_turn_ids), None)
        if removable is None:
            raise ContextOverflowError(decision)
        kept.remove(removable)
        dropped.append(removable.turn_id)
