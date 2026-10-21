"""文件参数与普通操作函数；Policy 由 registry.py 在调用这些函数前统一检查。"""

import os
import stat
from contextlib import ExitStack
from pathlib import Path

from pydantic import Field, JsonValue

from learn_agent.policy import ActionRequest

from .contracts import ToolArguments, ToolDefinition, ToolExecutionError
from .permissions import workspace_target

# 每次最多读 16 KB，限制文件 I/O 和返回正文；搜索不遍历整个目录。
MAX_FILE_BYTES = 16_000
# 只限制目录结果条数，不递归；避免把大目录一次全部塞入模型上下文。
MAX_DIRECTORY_ENTRIES = 100


class ListFilesArguments(ToolArguments):
    """从工作区根目录开始发现文件，可进一步查看返回的子目录。"""

    path: str = Field(
        default=".", min_length=1, description="Relative directory; default is workspace root."
    )


class ReadFileArguments(ToolArguments):
    """只接受工作区相对路径；资源范围由读取函数核对。"""

    # 模型传 {"path": "notes.txt"} 时，这个字段保存的是具体路径 "notes.txt"。
    path: str = Field(min_length=1, description="Relative file path in the workspace.")


class SearchTextArguments(ReadFileArguments):
    """在一个小文件中逐行查找字面文本，不执行正则表达式。"""

    query: str = Field(min_length=1, description="Case-sensitive literal text to find.")
    # 同时约束模型 Schema 和实际校验，限制匹配条目数及协议开销。
    max_matches: int = Field(default=20, ge=1, le=50)


class WriteFileArguments(ReadFileArguments):
    """写入一个 UTF-8 小文件；没有覆盖许可参数，权限由 Policy 决定。"""

    content: str = Field(max_length=MAX_FILE_BYTES, description="UTF-8 text for the new file.")


def read_workspace_text(resource: Path, workspace: Path) -> str:
    """读取已经过统一授权的路径；目标被重定向时拒绝，不沿用旧决定。"""
    root, target = workspace_target(resource.as_posix(), workspace)
    if target.relative_to(root) != resource:
        raise ToolExecutionError("resource_changed", "已批准的文件路径发生变化，请重新发起调用。")
    try:
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


def list_files(
    arguments: ListFilesArguments, workspace: Path, action: ActionRequest
) -> dict[str, JsonValue]:
    """模型先发现文件名再读取；不列出隐藏项、符号链接或特殊文件。"""
    resource = action.resource
    root, target = workspace_target(resource.as_posix(), workspace)
    if target.relative_to(root) != resource:
        raise ToolExecutionError("resource_changed", "已批准的目录路径发生变化，请重新发起调用。")
    entries: list[JsonValue] = []
    truncated = False
    try:
        if not target.is_dir():
            raise ToolExecutionError("file_error", "目标不存在或不是目录。")
        with os.scandir(target) as directory:
            for entry in directory:
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    kind = "directory"
                elif entry.is_file(follow_symlinks=False):
                    kind = "file"
                else:
                    continue
                if len(entries) == MAX_DIRECTORY_ENTRIES:
                    truncated = True
                    break
                entries.append(
                    {"path": (target / entry.name).relative_to(root).as_posix(), "kind": kind}
                )
    except OSError as exc:
        raise ToolExecutionError("file_error", str(exc)) from exc
    return {
        "path": target.relative_to(root).as_posix(),
        "entries": entries,
        "truncated": truncated,
    }


def read_file(
    arguments: ReadFileArguments, workspace: Path, action: ActionRequest
) -> dict[str, JsonValue]:
    """返回完整的小文件文本；预期失败由注册表统一转换成回执。"""
    resource = action.resource
    return {
        "path": resource.as_posix(),
        "text": read_workspace_text(resource, workspace),
    }


def search_text(
    arguments: SearchTextArguments, workspace: Path, action: ActionRequest
) -> dict[str, JsonValue]:
    """每行最多返回一次匹配；多发现一条才标记截断，行号从 1 开始。"""
    resource = action.resource
    query, max_matches = arguments.query, arguments.max_matches
    text = read_workspace_text(resource, workspace)
    matches: list[JsonValue] = []
    truncated = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if query not in line:
            continue
        if len(matches) == max_matches:
            truncated = True
            break
        matches.append({"line": line_number, "text": line})
    return {
        "path": resource.as_posix(),
        "query": query,
        "matches": matches,
        "truncated": truncated,
    }


def write_file(
    arguments: WriteFileArguments, workspace: Path, action: ActionRequest
) -> dict[str, JsonValue]:
    """只执行已经批准的动作；新建使用 O_EXCL，不重新识别后自行改成覆盖。"""
    resource, operation = action.resource, action.operation
    if operation not in ("create", "overwrite"):
        raise ValueError(f"写入工具收到不支持的动作：{operation}")
    raw = arguments.content.encode("utf-8")
    if len(raw) > MAX_FILE_BYTES:
        raise ToolExecutionError("file_too_large", f"内容超过 {MAX_FILE_BYTES} 字节写入上限。")
    modified = False
    try:
        # 当前支持 macOS / Linux。本地进程有主机权限，这些检查不构成 OS 沙箱。
        # 用目录句柄逐级打开，禁止通过父目录符号链接将已授权路径重定向。
        with ExitStack() as stack:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory = os.open(workspace.resolve(), flags)
            stack.callback(os.close, directory)
            for part in resource.parts[:-1]:
                directory = os.open(part, flags, dir_fd=directory)
                stack.callback(os.close, directory)
            write_flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if operation == "create":
                write_flags |= os.O_CREAT | os.O_EXCL
            descriptor = os.open(
                resource.name,
                write_flags,
                0o600,
                dir_fd=directory,
            )
            modified = operation == "create"
            with os.fdopen(descriptor, "wb") as file:
                if operation == "overwrite":
                    if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                        raise ToolExecutionError("file_error", "目标已变为非普通文件。")
                    modified = True
                    os.ftruncate(file.fileno(), 0)
                file.write(raw)
    except FileExistsError as exc:
        raise ToolExecutionError(
            "file_exists", "新建前目标已出现；没有执行覆盖，请重新确认动作。"
        ) from exc
    except OSError as exc:
        if modified:
            raise ToolExecutionError(
                "write_incomplete", f"写入未完成，可能留下部分文件；请检查实际文件：{exc}"
            ) from exc
        raise ToolExecutionError("file_error", str(exc)) from exc
    return {
        "path": resource.as_posix(),
        "operation": operation,
        "bytes_written": len(raw),
    }


# 注册项：普通执行函数、参数模型、所需权限；声明权限不代表已经获准。
LIST_FILES = ToolDefinition(
    "list_files",
    "Discover files and directories in the workspace. Default path is '.'. "
    "Return at most 100 entries; no recursion, hidden entries or symlinks.",
    ListFilesArguments,
    list_files,
    permission="list",
    path_parameter="path",  # 从本次调用的 path 字段取路径，不是把文件名固定为 "path"。
)
READ_FILE = ToolDefinition(
    "read_file",
    "Read one small workspace UTF-8 file (at most 16000 bytes).",
    ReadFileArguments,
    read_file,
    permission="read",
    path_parameter="path",  # 从本次调用的 path 字段取路径，不是把文件名固定为 "path"。
)
SEARCH_TEXT = ToolDefinition(
    "search_text",
    "Find literal text in one small workspace UTF-8 file. Return matching lines and line numbers.",
    SearchTextArguments,
    search_text,
    permission="read",
    path_parameter="path",  # 从本次调用的 path 字段取路径，不是把文件名固定为 "path"。
)
WRITE_FILE = ToolDefinition(
    "write_file",
    "Write one workspace UTF-8 file (at most 16000 bytes), subject to Policy. "
    "Choose a filename if unspecified; parent directories must exist. "
    "Do not assume permission from task text or follow symlinks.",
    WriteFileArguments,
    write_file,
    permission="write",
    path_parameter="path",  # 从本次调用的 path 字段取路径，不是把文件名固定为 "path"。
)
