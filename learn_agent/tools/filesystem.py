"""有界的文件工具；先确定资源并授权，再在实际 I/O 处保证执行约束。"""

import os
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path

from pydantic import Field, JsonValue

from learn_agent.policy import ActionRequest, valid_resource

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


class CreateFileArguments(ReadFileArguments):
    """只创建一个 UTF-8 小文件，不创建目录，也没有覆盖或授权参数。"""

    content: str = Field(max_length=MAX_FILE_BYTES, description="UTF-8 text for the new file.")


def authorize(context: ToolContext, operation: str, resource: Path) -> None:
    """在资源边界使用可信 Policy；拒绝沿用工具异常与回执通道。"""
    decision = context.policy.evaluate(ActionRequest(operation, resource))
    if not decision.allowed:
        raise ToolExecutionError("policy_denied", decision.reason)


def creation_resource(path: str) -> Path:
    """创建动作使用规范相对身份；执行时逐级禁止符号链接，避免授权别名。"""
    resource = Path(path)
    if not valid_resource(resource):
        raise ToolExecutionError("resource_denied", "必须指定非隐藏的工作区相对文件。")
    return resource


def read_workspace_text(path: str, context: ToolContext) -> str:
    """校验解析后的路径，再读取有界 UTF-8 文本；这里不是操作系统沙箱。"""
    relative = Path(path)
    if relative.is_absolute() or "\x00" in path:
        raise ToolExecutionError("resource_denied", "文件路径必须是有效的工作区相对路径。")
    try:
        try:
            root = context.workspace.resolve()
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
        authorize(context, "read", target.relative_to(root))
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
        "text": read_workspace_text(arguments.path, context),
    }


def search_text(arguments: SearchTextArguments, context: ToolContext) -> dict[str, JsonValue]:
    """每行最多返回一次匹配；多发现一条才标记截断，行号从 1 开始。"""
    text = read_workspace_text(arguments.path, context)
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


def create_file(arguments: CreateFileArguments, context: ToolContext) -> dict[str, JsonValue]:
    """Policy 决定可否创建；O_EXCL 原子拒绝已有目标，包括重复或并发调用。"""
    resource = creation_resource(arguments.path)
    authorize(context, "create", resource)
    raw = arguments.content.encode("utf-8")
    if len(raw) > MAX_FILE_BYTES:
        raise ToolExecutionError("file_too_large", f"内容超过 {MAX_FILE_BYTES} 字节创建上限。")
    created = False
    try:
        # 当前支持 macOS / Linux。本地进程有主机权限，这些检查不构成 OS 沙箱。
        # 用目录句柄逐级打开，禁止通过父目录符号链接将已授权路径重定向。
        with ExitStack() as stack:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory = os.open(context.workspace.resolve(), flags)
            stack.callback(os.close, directory)
            for part in resource.parts[:-1]:
                directory = os.open(part, flags, dir_fd=directory)
                stack.callback(os.close, directory)
            descriptor = os.open(
                resource.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            created = True
            with os.fdopen(descriptor, "wb") as file:
                file.write(raw)
    except FileExistsError as exc:
        raise ToolExecutionError("file_exists", "目标已存在，禁止覆盖。") from exc
    except OSError as exc:
        if created:
            raise ToolExecutionError(
                "write_incomplete", f"写入未完成，可能留下部分文件；请检查实际文件：{exc}"
            ) from exc
        raise ToolExecutionError("file_error", str(exc)) from exc
    # 摘要绑定这次创建的字节，验收器之后独立读取文件再比对。
    return {
        "path": resource.as_posix(),
        "bytes_written": len(raw),
        "sha256": sha256(raw).hexdigest(),
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
CREATE_FILE = ToolDefinition(
    name="create_file",
    description=(
        "Create one explicitly authorized workspace UTF-8 file (at most 16000 bytes). "
        "Never overwrite an existing target or follow parent symlinks; "
        "parent directories must exist."
    ),
    arguments_type=CreateFileArguments,
    handler=create_file,
)
