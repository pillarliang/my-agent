"""模型边界：Runner 提交统一请求，取得统一回复，不依赖供应商 SDK。"""

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..tools import ToolCall, ToolResult, ToolSpec


@dataclass(frozen=True)
class TextMessage:
    """应用指令与用户输入；工具结果直接使用已有的 ToolResult。"""

    role: Literal["system", "user"]
    content: str


@dataclass(frozen=True)
class TokenUsage:
    """记录供应商报告的用量；缺失字段保留 None，不把未知当成零。"""

    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class ProviderContinuation:
    """适配器保存原生续接材料，Runner 只保存与传回，不解释或修改。"""

    namespace: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ModelResponse:
    """一次完整模型响应；文字和工具调用可以同时存在。"""

    text: str | None
    tool_calls: tuple[ToolCall, ...]
    stop_reason: Literal["stop", "tool_calls", "length", "content_filter", "refusal"]
    model: str
    usage: TokenUsage | None = None
    refusal: str | None = None
    continuation: ProviderContinuation | None = None


type ModelMessage = TextMessage | ModelResponse | ToolResult


@dataclass(frozen=True)
class ModelRequest:
    """完整历史包含旧回复与工具回执；工具声明和输出额度属于本次请求。"""

    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolSpec, ...] = ()
    max_output_tokens: int = 512


class ModelError(RuntimeError):
    """模型访问失败；保留可判别的错误类型、状态码与原始异常链。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id


class ModelProtocolError(ModelError):
    """响应无法解释或续接材料不属于当前模型连接，停止而不猜测。"""

    def __init__(self, message: str) -> None:
        super().__init__("protocol_error", message)


class Model(Protocol):
    """Runner 使用的黑盒接口；一次 invoke 只负责一次模型请求。"""

    def invoke(self, request: ModelRequest) -> ModelResponse:
        """返回完整回复或抛出模型错误，不执行工具、不自动推进 Agent Loop。"""
        ...
