"""阶段 1 运行入口：在项目根目录执行 uv run python -m learn_agent。"""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import DefaultHttpxClient, OpenAI

from .runner import run_agent
from .tools import READ_FILE, SEARCH_TEXT, ToolContext, ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "examples/workspace"


def main() -> None:
    """应用负责选择工具和工作区；模型不能通过参数扩大工具集合或资源范围。"""
    tools = ToolRegistry([READ_FILE, SEARCH_TEXT], ToolContext(WORKSPACE))
    load_dotenv(ROOT / ".env", override=False, interpolate=False)
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    endpoint = os.environ.get("DASHSCOPE_BASE_URL")
    if not api_key or not endpoint:
        raise SystemExit("请在 .env 中配置 DASHSCOPE_API_KEY 和 DASHSCOPE_BASE_URL。")
    model = os.environ.get("DASHSCOPE_MODEL", "qwen3.8-flash")
    proxy = os.environ.get("DASHSCOPE_USE_ENV_PROXY", "true").lower()
    if proxy not in {"true", "false"}:
        raise SystemExit("DASHSCOPE_USE_ENV_PROXY 只能是 true 或 false。")
    # 禁用 SDK 自动重试，便于观察一次 Step 与一次请求的对应；超时限制网络等待。
    # SDK 和循环异常直接向外报告，保留诊断信息；上下文管理器始终关闭客户端。
    with OpenAI(
        api_key=api_key,
        base_url=endpoint,
        max_retries=0,
        timeout=15,
        http_client=DefaultHttpxClient(trust_env=proxy == "true"),
    ) as client:
        question = "请用 search_text 在 notes.txt 中查找‘验收标记’，回答标记内容并给出路径和行号。"
        answer = run_agent(client, model, question, tools)
    print(f"\n模型说明：\n{answer}")


if __name__ == "__main__":
    main()
