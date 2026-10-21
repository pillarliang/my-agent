"""启动时注册，运行时通过同一份定义完成声明、校验和本地分发。"""

import json
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from pydantic import JsonValue, ValidationError

from learn_agent.policy import ActionRequest, Policy, PolicyEvaluator

from .contracts import (
    ToolArguments,
    ToolCall,
    ToolDefinition,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
)
from .permissions import prepare_action, validate_permission


def failure(call: ToolCall, code: str, message: str) -> ToolResult:
    """所有可反馈失败都保留调用身份，并使用同一种结果形状。"""
    return ToolResult(
        call.call_id, call.name, {"ok": False, "error": {"code": code, "message": message}}
    )


class ToolRegistry:
    """本阶段的进程内执行器；启动时注册，所有工具共用参数校验、Policy 检查和执行流程。"""

    def __init__(
        self,
        definitions: Iterable[ToolDefinition],
        workspace: Path,
        policy: PolicyEvaluator | None = None,
    ) -> None:
        # 不同工具有不同参数类型；具体类型仍保存在各自定义中，统一在此校验。
        self._definitions: dict[str, ToolDefinition] = {}
        self._workspace = workspace
        self._policy = Policy() if policy is None else policy
        for definition in definitions:
            self.register(
                definition.function,
                definition.arguments_type,
                permission=definition.permission,
                path_parameter=definition.path_parameter,
                name=definition.name,
                description=definition.description,
            )

    def register(
        self,
        function: Callable[[Any, Path, ActionRequest], dict[str, JsonValue]],
        arguments_type: type[ToolArguments],
        *,
        permission: str,
        path_parameter: str | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        """保存普通函数、参数和权限声明；没有权限声明就不能注册。

        path_parameter 填参数模型中的字段名，不填具体文件名。
        例如填 "path"，收到 {"path": "notes.txt"} 后，从 arguments.path 取得 "notes.txt"。
        权限处理代码据此生成本次动作，再交给 Policy 判断是否允许。
        """
        tool_name = function.__name__ if name is None else name
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", tool_name):
            raise ValueError(f"非法工具名：{tool_name}")
        if tool_name in self._definitions:
            raise ValueError(f"工具重复注册：{tool_name}")
        if not callable(function):
            raise ValueError(f"工具必须提供执行函数：{tool_name}")
        validate_permission(permission, path_parameter, arguments_type)
        self._definitions[tool_name] = ToolDefinition(
            tool_name,
            (function.__doc__ or "").strip() if description is None else description,
            arguments_type,
            function,
            permission,
            path_parameter,
        )

    @property
    def specs(self) -> list[ToolSpec]:
        """从实际注册项生成声明，返回新字典，调用方不能修改分发表。"""
        return [
            {
                "name": definition.name,
                "description": definition.description,
                "parameters": definition.arguments_type.model_json_schema(),
            }
            for definition in self._definitions.values()
        ]

    def execute(self, call: ToolCall) -> ToolResult:
        """校验 → 识别动作 → 统一授权 → 执行；拒绝时不调用注册的执行函数。"""
        tool = self._definitions.get(call.name)
        if tool is None:
            return failure(call, "unknown_tool", f"工具未注册：{call.name}")
        try:
            raw = json.loads(call.arguments)
        except json.JSONDecodeError:
            return failure(call, "invalid_arguments", "工具参数必须是合法 JSON。")
        try:
            arguments = tool.arguments_type.model_validate(raw)
        except ValidationError as exc:
            # 不把原始输入复制到错误说明；给出字段位置与规则即可修正调用。
            details = "; ".join(
                f"{'.'.join(map(str, issue['loc'])) or 'arguments'}: {issue['msg']}"
                for issue in exc.errors(include_input=False, include_url=False)
            )
            return failure(call, "invalid_arguments", details)
        try:
            # 第一步：把“需要什么权限”具体化为“本次要对哪个目标做什么”。
            # 例如 count_lines 声明 read，调用参数 path="notes.txt"，
            # 生成 ActionRequest("read", Path("notes.txt"))；此时没有读取正文，也没有授权。
            action = prepare_action(
                tool.permission,  # 注册时声明的需求，如 read / write，不是允许或拒绝。
                tool.path_parameter,  # 注册时指定的路径参数名，如 "path"，不是文件名。
                arguments,  # 本次模型调用的参数，已通过参数模型校验，如 arguments.path。
                self._workspace,  # 应用创建注册表时设置的工作目录，用来确定实际目标。
                tool.name,  # 注册的工具名；compute 没有文件路径，用它作为目标标识。
            )
            # 第二步：用创建注册表时传入的 Policy，判断刚才这个动作是否允许。
            # evaluate 只检查规则，返回 PolicyDecision(allowed, reason)，不操作文件。
            decision = self._policy.evaluate(action)
            # 拒绝就返回失败回执；执行函数不会运行，因此不会读取或写入正文。
            if not decision.allowed:
                return failure(call, "policy_denied", decision.reason)
            # 获准后才执行，并传入已批准的目标与动作；例如按 create 执行，不能改成覆盖。
            data = tool.function(arguments, self._workspace, action)
        except ToolExecutionError as exc:
            return failure(call, exc.code, str(exc))
        return ToolResult(call.call_id, call.name, {"ok": True, "data": data})
