"""阶段 1 运行入口：在项目根目录执行 uv run python -m learn_agent。"""

from pathlib import Path

from .models import LLMService
from .runner import run_agent
from .tools import READ_FILE, SEARCH_TEXT, ToolContext, ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "examples/workspace"


def main() -> None:
    """应用负责选择工具和工作区；模型不能通过参数扩大工具集合或资源范围。"""
    tools = ToolRegistry([READ_FILE, SEARCH_TEXT], ToolContext(WORKSPACE))
    model = LLMService()
    try:
        question = "请用 search_text 在 notes.txt 中查找‘验收标记’，回答标记内容并给出路径和行号。"
        answer = run_agent(model, question, tools)
    finally:
        model.close()
    print(f"\n模型说明：\n{answer}")


if __name__ == "__main__":
    main()
