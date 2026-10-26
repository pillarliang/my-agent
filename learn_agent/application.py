"""注册工具、创建模型并运行 Runner，最后关闭模型连接。"""

from collections.abc import Iterable
from pathlib import Path

from .context import DEFAULT_BUDGET, ContextBudget
from .models import LLMService
from .policy import PolicyEvaluator
from .runner import RunResult, run_agent
from .session import SessionLog
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
    session: SessionLog | None = None,
    context_budget: ContextBudget = DEFAULT_BUDGET,
    required_turn_ids: frozenset[str] = frozenset(),
) -> RunResult:
    """会话由入口持有；校验工作区后运行任务，最后关闭模型连接。"""
    if session is not None:
        session.check_workspace(workspace)
    tools = ToolRegistry(definitions, workspace, policy)
    model = LLMService()
    try:
        return run_agent(
            model,
            question,
            tools,
            session=session,
            context_budget=context_budget,
            required_turn_ids=required_turn_ids,
        )
    finally:
        model.close()
