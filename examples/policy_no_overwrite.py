"""Policy 实验：通过真实工具调用验证禁止覆盖，不调用模型。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from learn_agent.policy import Policy
from learn_agent.tools import WRITE_FILE, ToolCall, ToolRegistry


def main() -> int:
    with TemporaryDirectory(prefix="learn-agent-policy-") as directory:
        workspace = Path(directory)
        tools = ToolRegistry([WRITE_FILE], workspace, Policy(allow_create=True))
        original = tools.execute(
            ToolCall(
                "first", "write_file", json.dumps({"path": "summary.txt", "content": "原内容"})
            )
        )
        assert original.content["ok"]
        denied = tools.execute(
            ToolCall(
                "second", "write_file", json.dumps({"path": "summary.txt", "content": "新内容"})
            )
        )
        assert not denied.content["ok"] and denied.content["error"]["code"] == "policy_denied"
        assert (workspace / "summary.txt").read_text(encoding="utf-8") == "原内容"
        print(json.dumps(denied.content, ensure_ascii=False))
        print("验证通过：Policy 拒绝覆盖，文件仍为“原内容”。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
