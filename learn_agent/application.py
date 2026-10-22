"""注册工具、创建模型并运行 Runner，最后关闭模型连接。"""

from collections.abc import Iterable
from pathlib import Path

from .models import LLMService
from .policy import PolicyEvaluator
from .runner import RunResult, run_agent
from .state import SessionStore
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
    store: SessionStore | None = None,
    session_id: str | None = None,
) -> RunResult:
    """注册工具并运行任务；存储连接由入口管理，模型连接在这里关闭。"""
    if (store is None) != (session_id is None):
        raise ValueError("持久运行需要同时传入 store 和 session_id。")
    if store is not None and session_id is not None:
        # 先核对会话对应的工作目录，错误时不创建模型连接或执行任何工具。
        store.check_workspace(session_id, workspace)
    tools = ToolRegistry(definitions, workspace, policy)
    model = LLMService()
    try:
        return run_agent(
            model,
            question,
            tools,
            store=store,
            session_id=session_id,
        )
    finally:
        model.close()
