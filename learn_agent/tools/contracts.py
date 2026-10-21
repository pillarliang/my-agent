"""工具边界：声明、调用与回执，不依赖模型 SDK。"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, JsonValue

from learn_agent.policy import ActionRequest


class ToolArguments(BaseModel):
    """参数模型同时生成 Schema 和校验输入，禁止额外字段与隐式类型转换。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ToolSpec(TypedDict):
    """模型可见声明；如何编码成供应商协议由调用方负责。"""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolDefinition:
    """普通函数、参数模型与所需权限组成注册项；权限是数据，不是回调。"""

    name: str
    description: str
    arguments_type: type[ToolArguments]
    # 各工具参数模型不同；具体函数保留各自的类型，注册表统一负责校验。
    function: Callable[[Any, Path, ActionRequest], dict[str, JsonValue]]
    permission: str  # 工具需要的权限；最终是否允许仍由 Policy 判断。
    # 从哪个调用参数取文件路径。填 "path" 表示读 arguments.path，不是固定读某个文件。
    # 无文件路径的 compute 权限不填写此项。
    path_parameter: str | None = None


@dataclass(frozen=True)
class ToolCall:
    """保留模型生成的原始 JSON 与调用 ID，执行器负责解析参数。"""

    call_id: str
    name: str
    arguments: str


class ToolSuccess(TypedDict):
    """成功只说明这个动作完成，不代表整个任务已经验收。"""

    ok: Literal[True]
    data: dict[str, JsonValue]


class ToolError(TypedDict):
    """稳定错误码供程序区分，说明文字供模型理解和修正。"""

    code: str
    message: str


class ToolFailure(TypedDict):
    """已知的调用失败可以进入下一轮模型输入。"""

    ok: Literal[False]
    error: ToolError


@dataclass(frozen=True)
class ToolResult:
    """调用身份与结果分开保存，由 Loop 编码成对应协议的工具消息。"""

    call_id: str
    name: str
    content: ToolSuccess | ToolFailure


class ToolExecutionError(Exception):
    """工具主动报告的预期失败；未声明的程序异常不在注册表中吞掉。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ToolExecutor(Protocol):
    """Loop 实际依赖的接口；以后可替换执行器，不预设沙箱或 RPC 实现。"""

    @property
    def specs(self) -> list[ToolSpec]:
        """取得本次运行可用的工具声明。"""
        ...

    def execute(self, call: ToolCall) -> ToolResult:
        """执行一次调用，回执保留原调用 ID；程序错误可以向外抛出。"""
        ...
