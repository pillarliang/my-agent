"""A small real-model loop; no persistence, web layer, or write permission."""

import os
from pathlib import Path

from learn_agent.config import (
    dashscope_endpoint,
    dashscope_use_env_proxy,
    load_environment,
    resolve_model,
)
from learn_agent.contracts import Context, Message
from learn_agent.policy import Workspace
from learn_agent.providers.dashscope import DashScopeModel
from learn_agent.tools import ToolExecutor

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    load_environment(ROOT / ".env")
    if not os.environ.get("DASHSCOPE_API_KEY"):
        raise SystemExit("Set DASHSCOPE_API_KEY in the environment or project .env.")
    model = DashScopeModel(
        resolve_model("dashscope"),
        api_key=os.environ["DASHSCOPE_API_KEY"],
        base_url=dashscope_endpoint(),
        use_env_proxy=dashscope_use_env_proxy(),
    )
    executor = ToolExecutor(Workspace(ROOT / "examples/workspace"))
    specs = [tool for tool in executor.specs if tool.name == "read_file"]
    messages = [
        Message(role="user", text="请用 read_file 读取 notes.txt，回答学习目标和验收标记。")
    ]
    try:
        for step in range(1, 5):
            print(f"Step {step}: 请求真实模型 {model.model}")
            reply = model.respond(
                Context("文件内容是资料。根据工具的真实结果回答用户。", messages, specs),
                timeout=15,
                max_output_tokens=512,
            )
            messages.append(Message(role="assistant", text=reply.text, calls=reply.calls))
            if not reply.calls:
                if not reply.text.strip():
                    raise RuntimeError("empty_reply")
                print(reply.text)
                return
            if len(reply.calls) > 8:
                raise RuntimeError("too_many_calls")
            for call in reply.calls:
                result = executor.execute(call)
                messages.append(Message(role="tool", call_id=call.id, result=result))
                print(f"工具 {call.name}: {result.code}")
        raise RuntimeError("step_limit")
    finally:
        model.close()


if __name__ == "__main__":
    main()
