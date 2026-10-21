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
class Policy:
    """可信应用为一个工作区配置的规则；默认只读，创建权限精确到文件。"""

    allow_read: bool = True
    create_files: frozenset[Path] = frozenset()


@dataclass(frozen=True)
class ActionRequest:
    """工具整理后的动作与资源；不把模型给出的 approved 等字段当成授权。"""

    operation: str
    resource: Path


@dataclass(frozen=True)
class PolicyDecision:
    """允许或拒绝及原因；允许创建不表示文件不存在，也不保证操作成功。"""

    allowed: bool
    reason: str


class PolicyEvaluator(Protocol):
    """工具依赖的接口；后续可替换规则实现，Runner 无需参与判断。"""

    def evaluate(self, action: ActionRequest) -> PolicyDecision: ...


class PolicyService:
    """第一版只匹配读取与指定文件创建；其他动作默认拒绝。"""

    def __init__(self, policy: Policy | None = None) -> None:
        self._policy = policy if policy is not None else Policy()
        for resource in self._policy.create_files:
            if not valid_resource(resource):
                raise ValueError(f"创建授权必须指定非隐藏的工作区相对文件：{resource}")

    def evaluate(self, action: ActionRequest) -> PolicyDecision:
        """规则只来自构造时的 Policy；模型文本与工具参数不能修改它。"""
        if not valid_resource(action.resource):
            return PolicyDecision(False, "资源必须是非隐藏的工作区相对文件。")
        if action.operation == "read" and self._policy.allow_read:
            return PolicyDecision(True, "允许读取工作区文件。")
        if action.operation == "create" and action.resource in self._policy.create_files:
            return PolicyDecision(True, "允许新建指定文件；执行器仍须保证不覆盖。")
        return PolicyDecision(False, f"未授权动作：{action.operation} {action.resource.as_posix()}")
