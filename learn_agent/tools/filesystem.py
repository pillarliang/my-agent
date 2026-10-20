"""两个有界的只读工具；文件访问规则在真实资源边界检查。"""

from pathlib import Path

from pydantic import Field, JsonValue

from .contracts import ToolArguments, ToolContext, ToolDefinition, ToolExecutionError

# 每次最多读 16 KB，限制文件 I/O 和返回正文；搜索不遍历整个目录。
MAX_FILE_BYTES = 16_000


class ReadFileArguments(ToolArguments):
    """只接受工作区相对路径；资源范围由读取函数核对。"""

    path: str = Field(min_length=1, description="Relative file path in the workspace.")


class SearchTextArguments(ReadFileArguments):
    """在一个小文件中逐行查找字面文本，不执行正则表达式。"""

    query: str = Field(min_length=1, description="Case-sensitive literal text to find.")
    # 同时约束模型 Schema 和实际校验，限制匹配条目数及协议开销。
    max_matches: int = Field(default=20, ge=1, le=50)


def read_workspace_text(path: str, workspace: Path) -> str:
    """校验解析后的路径，再读取有界 UTF-8 文本；这里不是操作系统沙箱。"""
    relative = Path(path)
    if relative.is_absolute() or "\x00" in path:
        raise ToolExecutionError("resource_denied", "文件路径必须是有效的工作区相对路径。")
    try:
        try:
            root = workspace.resolve()
            target = (root / relative).resolve()
        except RuntimeError as exc:
            # Python 3.12 用 RuntimeError 报告符号链接循环，只在路径解析处转换。
            raise ToolExecutionError("file_error", "路径包含无法解析的符号链接循环。") from exc
        if not target.is_relative_to(root):
            raise ToolExecutionError("resource_denied", "文件路径必须位于工作区内。")
        # 同时检查模型给出的路径与符号链接目标，禁止借别名读取隐藏文件。
        parts = (*relative.parts, *target.relative_to(root).parts)
        if any(part.startswith(".") for part in parts):
            raise ToolExecutionError("resource_denied", "不能读取隐藏路径。")
        if not target.is_file():
            raise ToolExecutionError("file_error", "目标不存在或不是普通文件。")
        with target.open("rb") as file:
            raw = file.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise ToolExecutionError("file_error", str(exc)) from exc
    if len(raw) > MAX_FILE_BYTES:
        raise ToolExecutionError("file_too_large", f"文件超过 {MAX_FILE_BYTES} 字节读取上限。")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolExecutionError("invalid_encoding", "文件不是有效的 UTF-8 文本。") from exc


def read_file(arguments: ReadFileArguments, context: ToolContext) -> dict[str, JsonValue]:
    """返回完整的小文件文本；预期失败由注册表统一转换成回执。"""
    return {
        "path": arguments.path,
        "text": read_workspace_text(arguments.path, context.workspace),
    }


def search_text(arguments: SearchTextArguments, context: ToolContext) -> dict[str, JsonValue]:
    """每行最多返回一次匹配；多发现一条才标记截断，行号从 1 开始。"""
    text = read_workspace_text(arguments.path, context.workspace)
    matches: list[JsonValue] = []
    truncated = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if arguments.query not in line:
            continue
        if len(matches) == arguments.max_matches:
            truncated = True
            break
        matches.append({"line": line_number, "text": line})
    return {
        "path": arguments.path,
        "query": arguments.query,
        "matches": matches,
        "truncated": truncated,
    }


READ_FILE = ToolDefinition(
    name="read_file",
    description="Read one small UTF-8 text file in the workspace (at most 16000 bytes).",
    arguments_type=ReadFileArguments,
    handler=read_file,
)
SEARCH_TEXT = ToolDefinition(
    name="search_text",
    description=(
        "Find case-sensitive literal text in one workspace UTF-8 file (at most 16000 bytes). "
        "Return matching lines with 1-based line numbers and a truncation flag."
    ),
    arguments_type=SearchTextArguments,
    handler=search_text,
)
