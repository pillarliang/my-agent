"""阶段 5：读取当前工作区的人工项目记忆，每次任务保留一份不可变快照。"""

import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectMemory:
    """来源使用工作区相对路径；正文是任务启动时读取的完整 UTF-8 文本。"""

    source: str
    content: str


def load_project_memory(workspace: Path) -> ProjectMemory | None:
    """只查找根目录 MEMORY.md；缺失或空白可省略，其他读取错误直接传播。"""
    root = workspace.resolve(strict=True)
    path = root / "MEMORY.md"
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    # 先区分真正缺失与损坏链接，再核对链接目标范围；不查找父目录记忆。
    target = path.resolve(strict=True)
    if not target.is_relative_to(root):
        raise ValueError(f"项目记忆必须位于当前工作区内：{path}")
    if not stat.S_ISREG(target.stat().st_mode):
        raise ValueError(f"项目记忆必须是普通文件：{path}")
    # 不套用工具的小文件截断规则；全文和来源一起由 Context 检查预算。
    content = target.read_bytes().decode("utf-8")
    return ProjectMemory(path.relative_to(root).as_posix(), content) if content.strip() else None
