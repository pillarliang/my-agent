"""注册工具、创建模型并运行 Runner，最后关闭模型连接。"""

from collections.abc import Iterable
from pathlib import Path

from .models import LLMService
from .policy import PolicyEvaluator
from .runner import RunResult, run_agent
from .tools import (
    LIST_FILES,
    READ_FILE,
    SEARCH_TEXT,
    WRITE_FILE,
    ToolDefinition,
    ToolRegistry,
)

DEFAULT_TOOLS = (LIST_FILES, READ_FILE, SEARCH_TEXT, WRITE_FILE)


def run_task(
    question: str,
    workspace: Path,
    policy: PolicyEvaluator,
    *,
    definitions: Iterable[ToolDefinition] = DEFAULT_TOOLS,
) -> RunResult:
    """只运行任务，返回模型回答与工具记录。"""
    tools = ToolRegistry(definitions, workspace, policy)
    model = LLMService()
    try:
        return run_agent(model, question, tools)
    finally:
        model.close()
