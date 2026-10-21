"""把注册时的权限声明与本次参数转换成动作；这里只检查目标，不读写正文。"""

import stat
from pathlib import Path

from learn_agent.policy import ActionRequest, valid_resource

from .contracts import ToolArguments, ToolExecutionError


def validate_permission(
    permission: str, path_parameter: str | None, arguments_type: type[ToolArguments]
) -> None:
    """注册时检查声明，漏写路径或把数值参数当路径都应立即报错。"""
    if permission not in {"read", "write", "list", "compute"}:
        raise ValueError(f"不支持的工具权限：{permission}")
    if permission == "compute":
        if path_parameter is not None:
            raise ValueError("compute 权限不使用路径参数。")
        return
    field = arguments_type.model_fields.get(path_parameter) if path_parameter else None
    if field is None or field.annotation is not str:
        raise ValueError(f"{permission} 权限必须指定参数模型中的字符串路径字段。")


def write_resource(path: str) -> Path:
    """写入动作使用规范相对路径；实际执行时逐级禁止符号链接。"""
    resource = Path(path)
    if not valid_resource(resource):
        raise ToolExecutionError("resource_denied", "必须指定非隐藏的工作区相对文件。")
    return resource


def workspace_target(path: str, workspace: Path) -> tuple[Path, Path]:
    """读取与目录查看共用路径检查；解析结果和原始路径都不能越界或指向隐藏项。"""
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
            raise ToolExecutionError("resource_denied", "不能访问隐藏路径。")
        return root, target
    except OSError as exc:
        raise ToolExecutionError("file_error", str(exc)) from exc


def prepare_action(
    permission: str,
    path_parameter: str | None,
    arguments: ToolArguments,
    workspace: Path,
    tool_name: str,
) -> ActionRequest:
    """所有使用同一种权限的工具共用解析，不依赖任何具体工具名。"""
    if permission == "compute":
        # 无文件路径的计算仍交给 Policy；资源标识为注册时确定的工具名。
        return ActionRequest("compute", Path(tool_name))
    if path_parameter is None:
        raise ValueError("文件权限缺少路径参数声明。")
    # path_parameter 保存的是字段名；例如它为 "path"，这里取出的就是 arguments.path。
    path = getattr(arguments, path_parameter)
    # write 只是写入需求。先查看目标是否存在，才能向 Policy 提交新建或覆盖动作。
    if permission == "write":
        resource = write_resource(path)
        try:
            target = (workspace / resource).lstat()
        except FileNotFoundError:
            operation = "create"
        except OSError as exc:
            raise ToolExecutionError("file_error", str(exc)) from exc
        else:
            if not stat.S_ISREG(target.st_mode):
                raise ToolExecutionError("file_exists", "目标不是普通文件，不能写入。")
            operation = "overwrite"
        return ActionRequest(operation, resource)
    # 读取与目录查看先确认真实路径位于工作目录内；这里只检查路径与文件类型。
    root, target_path = workspace_target(path, workspace)
    if permission == "list":
        if not target_path.is_dir():
            raise ToolExecutionError("file_error", "目标不存在或不是目录。")
    elif permission == "read":
        if not target_path.is_file():
            raise ToolExecutionError("file_error", "目标不存在或不是普通文件。")
    else:
        raise ValueError(f"不支持的工具权限：{permission}")
    # Policy 接收工作目录内的相对路径，例如 notes.txt，而不是绝对路径。
    return ActionRequest(permission, target_path.relative_to(root))
