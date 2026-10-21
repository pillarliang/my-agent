"""授权边界：应用提供规则，工具提供实际动作；本模块只作决定，不执行 I/O。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


def valid_resource(resource: Path) -> bool:
    """资源身份是当前工作区内的规范相对路径，不接受隐藏路径或向上跳转。"""
    return (
        bool(resource.parts)
        and not resource.is_absolute()
        and "\x00" not in str(resource)
        and all(not part.startswith(".") for part in resource.parts)
    )


@dataclass(frozen=True)
class ActionRequest:
    """Tools 识别的真实动作与工作区相对路径；动作不由模型自行声明。"""

    operation: str  # list / read / create / overwrite；未知动作拒绝。
    resource: Path  # 文件动作是工作区相对路径；compute 使用工具名标识。


@dataclass(frozen=True)
class PolicyDecision:
    """允许或拒绝及原因；允许只表示可以尝试执行，不保证执行成功。"""

    allowed: bool
    reason: str


class PolicyEvaluator(Protocol):
    """Tools 提交动作与路径，Policy 返回决定；Runner 不参与权限判断。"""

    def evaluate(self, action: ActionRequest) -> PolicyDecision: ...


@dataclass(frozen=True)
class Policy:
    """保存权限规则并作判断，不读写文件；默认只读且禁止覆盖。"""

    allow_read: bool = True
    create_files: frozenset[Path] = frozenset()
    allow_create: bool = False
    allow_overwrite: bool = False

    def __post_init__(self) -> None:
        for resource in self.create_files:
            if not valid_resource(resource):
                raise ValueError(f"创建授权必须指定非隐藏的工作区相对文件：{resource}")

    def evaluate(self, action: ActionRequest) -> PolicyDecision:
        """Tools 提交动作与路径，返回允许或拒绝及原因；未知动作拒绝。"""
        listing_root = action.operation == "list" and action.resource == Path(".")
        if not listing_root and not valid_resource(action.resource):
            return PolicyDecision(False, "资源必须是非隐藏的工作区相对文件。")
        if action.operation in ("read", "list") and self.allow_read:
            return PolicyDecision(True, "允许查看工作区文件。")
        if action.operation == "create" and (
            self.allow_create or action.resource in self.create_files
        ):
            return PolicyDecision(True, "允许新建此文件。")
        if action.operation == "overwrite":
            return PolicyDecision(
                self.allow_overwrite, "允许覆盖。" if self.allow_overwrite else "禁止覆盖已有文件。"
            )
        return PolicyDecision(False, f"未授权动作：{action.operation} {action.resource.as_posix()}")
