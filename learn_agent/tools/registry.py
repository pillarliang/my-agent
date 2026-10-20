"""启动时注册，运行时通过同一份定义完成声明、校验和本地分发。"""

import json
import re
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from .contracts import (
    ToolCall,
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
)


def failure(call: ToolCall, code: str, message: str) -> ToolResult:
    """所有可反馈失败都保留调用身份，并使用同一种结果形状。"""
    return ToolResult(
        call.call_id, call.name, {"ok": False, "error": {"code": code, "message": message}}
    )


class ToolRegistry:
    """本阶段的进程内执行器；注册后不提供运行中增删工具的接口。"""

    def __init__(self, definitions: Iterable[ToolDefinition[Any]], context: ToolContext) -> None:
        # 不同工具有不同参数类型；具体类型仍保存在各自定义中，统一在此校验。
        self._definitions: dict[str, ToolDefinition[Any]] = {}
        self._context = context
        for definition in definitions:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", definition.name):
                raise ValueError(f"非法工具名：{definition.name}")
            if definition.name in self._definitions:
                raise ValueError(f"工具重复注册：{definition.name}")
            self._definitions[definition.name] = definition

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
        """查找 → 解析与校验 → 执行；仅转换规则明确允许反馈的异常。"""
        definition = self._definitions.get(call.name)
        if definition is None:
            return failure(call, "unknown_tool", f"工具未注册：{call.name}")
        try:
            raw = json.loads(call.arguments)
        except json.JSONDecodeError:
            return failure(call, "invalid_arguments", "工具参数必须是合法 JSON。")
        try:
            arguments = definition.arguments_type.model_validate(raw)
        except ValidationError as exc:
            # 不把原始输入复制到错误说明；给出字段位置与规则即可修正调用。
            details = "; ".join(
                f"{'.'.join(map(str, issue['loc'])) or 'arguments'}: {issue['msg']}"
                for issue in exc.errors(include_input=False, include_url=False)
            )
            return failure(call, "invalid_arguments", details)
        try:
            data = definition.handler(arguments, self._context)
        except ToolExecutionError as exc:
            return failure(call, exc.code, str(exc))
        return ToolResult(call.call_id, call.name, {"ok": True, "data": data})
