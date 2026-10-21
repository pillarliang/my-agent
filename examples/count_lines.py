"""普通函数声明读取权限后注册为工具；本例不调用模型。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import JsonValue

from learn_agent.policy import ActionRequest, Policy
from learn_agent.tools import ToolCall, ToolRegistry
from learn_agent.tools.filesystem import ReadFileArguments, read_workspace_text


def count_lines(
    arguments: ReadFileArguments, workspace: Path, action: ActionRequest
) -> dict[str, JsonValue]:
    """Count lines in a small workspace file."""
    text = read_workspace_text(action.resource, workspace)
    return {"lines": len(text.splitlines())}


def main() -> int:
    with TemporaryDirectory(prefix="learn-agent-count-lines-") as directory:
        workspace = Path(directory)
        (workspace / "notes.txt").write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
        call = ToolCall("count_1", "count_lines", '{"path":"notes.txt"}')
        for allow_read in (True, False):
            tools = ToolRegistry([], workspace, Policy(allow_read=allow_read))
            tools.register(
                count_lines,
                ReadFileArguments,
                permission="read",  # 声明需要读取权限，是否允许由上面的 Policy 决定。
                # 这里填参数名。上面的调用传 {"path":"notes.txt"}，实际读取目标是 notes.txt。
                path_parameter="path",
            )
            result = tools.execute(call)
            print(json.dumps(result.content, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
