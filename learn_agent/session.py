"""阶段 3A：一会话一份 JSONL，保存任务边界与完整消息，重建已完成历史。"""

import fcntl
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self, TextIO
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .models import ModelMessage, ModelResponse, TextMessage
from .tools import ToolCall, ToolResult


class SessionHeader(BaseModel):
    """文件头只保存会话归属与格式版本，不保存模型连接或本次运行对象。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    type: Literal["session"]
    version: Literal[1]
    session_id: str
    workspace: str
    created_at: str


class Record(BaseModel):
    """有序记录的外层包装；payload 保存任务结论或带类型标签的消息。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    seq: int = Field(ge=1)
    turn_id: str
    type: Literal["turn_started", "message", "turn_finished"]
    time: str
    payload: dict[str, Any]


USER = TypeAdapter(TextMessage)
ASSISTANT = TypeAdapter(ModelResponse)
TOOL = TypeAdapter(ToolResult)


def now() -> str:
    return datetime.now(UTC).isoformat()


def session_path(directory: Path, session_id: str) -> Path:
    """只接受程序生成的 ID，避免把外部传入的路径作为会话文件打开。"""
    if re.fullmatch(r"[0-9a-f]{32}", session_id) is None:
        raise ValueError("会话 ID 必须是 32 位小写十六进制字符串。")
    return directory / f"{session_id}.jsonl"


def _header(file: TextIO) -> SessionHeader:
    line = file.readline()
    if not line.endswith("\n"):
        raise ValueError("会话文件头缺失或不完整。")
    header = SessionHeader.model_validate_json(line)
    if Path(file.name).stem != header.session_id:
        raise ValueError("会话文件名与文件头的 ID 不一致。")
    return header


def _read(file: TextIO) -> tuple[SessionHeader, list[Record]]:
    header = _header(file)
    records: list[Record] = []
    for line_number, line in enumerate(file, 2):
        if not line.endswith("\n"):
            raise ValueError(f"会话第 {line_number} 行不完整；请核对原记录，不自动修补。")
        record = Record.model_validate_json(line)
        if record.seq != len(records) + 1:
            raise ValueError(f"会话第 {line_number} 行的序号不连续。")
        records.append(record)
    return header, records


def _message(payload: dict[str, Any]) -> ModelMessage:
    """读取本地文件时按明确标签还原类型，不让存储层猜测供应商协议。"""
    if set(payload) != {"kind", "value"}:
        raise ValueError("消息记录需要 kind 与 value。")
    match payload["kind"]:
        case "user":
            message = USER.validate_python(payload["value"])
            if message.role != "user":
                raise ValueError("历史用户消息不能作为系统指令加载。")
            return message
        case "assistant":
            return ASSISTANT.validate_python(payload["value"])
        case "tool_result":
            return TOOL.validate_python(payload["value"])
        case _:
            raise ValueError("未知的历史消息类型。")


def _validate_completed(messages: list[ModelMessage]) -> None:
    """completed 历史必须是用户问题、完整调用回执链与最终回答。"""
    if not messages or not isinstance(messages[0], TextMessage) or not messages[0].content.strip():
        raise ValueError("已完成任务缺少用户问题。")
    pending: list[ToolCall] = []
    final = False
    for message in messages[1:]:
        if final:
            raise ValueError("最终回答之后还有消息。")
        if isinstance(message, ModelResponse):
            if pending:
                raise ValueError("已完成任务缺少工具回执。")
            if message.refusal or message.stop_reason not in ("stop", "tool_calls"):
                raise ValueError("已完成任务包含未正常结束的模型响应。")
            pending = list(message.tool_calls)
            if len({call.call_id for call in pending}) != len(pending):
                raise ValueError("同一模型响应中有重复的工具调用 ID。")
            if not pending:
                if not (message.text or "").strip():
                    raise ValueError("已完成任务的最终回答为空。")
                final = True
        elif isinstance(message, ToolResult):
            if not pending or (message.call_id, message.name) != (
                pending[0].call_id,
                pending[0].name,
            ):
                raise ValueError("工具回执与调用不匹配。")
            pending.pop(0)
        else:
            raise ValueError("一个用户任务中不能插入另一个用户问题。")
    if pending or not final:
        raise ValueError("已完成任务的消息链不完整。")


def _history(records: list[Record]) -> tuple[ModelMessage, ...]:
    """失败或未结束片段保留在记录中，只将完整 completed 任务带入模型。"""
    history: list[ModelMessage] = []
    messages: list[ModelMessage] = []
    active: str | None = None
    seen = set()
    for record in records:
        if record.type == "turn_started":
            if record.turn_id in seen or record.payload:
                raise ValueError("任务开始记录重复或含有未知数据。")
            seen.add(record.turn_id)
            # 新任务可接在中断片段后；这不会续跑或重做旧任务的动作。
            active, messages = record.turn_id, []
        elif active != record.turn_id:
            raise ValueError("消息或结束记录没有对应的活动任务。")
        elif record.type == "message":
            messages.append(_message(record.payload))
        else:
            match record.payload:
                case {"status": "completed"} if set(record.payload) == {"status"}:
                    _validate_completed(messages)
                    history.extend(messages)
                case {"status": "failed", "error": {"code": str(), "message": str()}}:
                    if set(record.payload) != {"status", "error"} or set(
                        record.payload["error"]
                    ) != {"code", "message"}:
                        raise ValueError("失败任务结论含有未知字段。")
                case _:
                    raise ValueError("任务结束记录需要明确的状态与失败原因。")
            active, messages = None, []
    return tuple(history)


class SessionLog:
    """持有一个会话文件与独占写锁；入口用 with 管理整个任务的写入生命周期。"""

    def __init__(
        self, directory: Path, *, session_id: str | None = None, workspace: Path | None = None
    ) -> None:
        creating = session_id is None
        if creating and workspace is None:
            raise ValueError("新会话需要工作目录。")
        session_id = uuid4().hex if session_id is None else session_id
        self.path = session_path(directory, session_id)
        if creating:
            directory.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("x+" if creating else "r+", encoding="utf-8")
        self.write_failed = False
        self._active: str | None = None
        try:
            try:
                fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("会话正在由另一个执行者使用，请稍后再试。") from exc
            if creating:
                assert workspace is not None
                self.header = SessionHeader(
                    type="session",
                    version=1,
                    session_id=session_id,
                    workspace=str(workspace.resolve()),
                    created_at=now(),
                )
                self._file.write(self.header.model_dump_json() + "\n")
                self._file.flush()
                self.records: list[Record] = []
            else:
                self.header, self.records = _read(self._file)
            self._completed = _history(self.records)
            self._file.seek(0, 2)
        except BaseException:
            self._file.close()
            raise

    def check_workspace(self, workspace: Path) -> None:
        if str(workspace.resolve()) != self.header.workspace:
            raise ValueError("会话工作目录与当前工作目录不一致。")

    def history(self) -> tuple[ModelMessage, ...]:
        return self._completed

    def _record(
        self,
        turn_id: str,
        kind: Literal["turn_started", "message", "turn_finished"],
        payload: dict[str, Any],
    ) -> Record:
        return Record(
            seq=len(self.records) + 1, turn_id=turn_id, type=kind, time=now(), payload=payload
        )

    def _append(self, record: Record) -> None:
        # 先序列化，再写入并 flush；写入失败后停止追加，避免把残行当作完整记录继续写。
        if self.write_failed:
            raise OSError("会话写入已失败，不能继续追加。")
        line = record.model_dump_json() + "\n"
        try:
            self._file.write(line)
            self._file.flush()
        except BaseException:
            self.write_failed = True
            raise
        self.records.append(record)

    def start_turn(self, question: str) -> str:
        if self._active is not None:
            raise ValueError("当前连接已有未结束的任务。")
        turn_id = uuid4().hex
        self._active = turn_id
        self._append(self._record(turn_id, "turn_started", {}))
        self.append_message(turn_id, TextMessage("user", question))
        return turn_id

    def append_message(self, turn_id: str, message: ModelMessage) -> None:
        if self._active != turn_id:
            raise ValueError("只能为当前任务保存消息。")
        if isinstance(message, TextMessage):
            if message.role != "user":
                raise ValueError("系统指令由当前应用提供，不追加到历史。")
            kind = "user"
        else:
            kind = "assistant" if isinstance(message, ModelResponse) else "tool_result"
        self._append(self._record(turn_id, "message", {"kind": kind, "value": asdict(message)}))

    def finish_turn(self, turn_id: str, *, error: dict[str, str] | None = None) -> None:
        if self._active != turn_id:
            raise ValueError("只能结束当前任务。")
        payload: dict[str, Any] = {"status": "completed"}
        if error is not None:
            payload = {"status": "failed", "error": error}
        record = self._record(turn_id, "turn_finished", payload)
        # 校验完整消息后才宣告完成；历史重建与普通详情查询各有用途。
        completed = _history([*self.records, record])
        self._append(record)
        self._completed, self._active = completed, None

    def close(self) -> None:
        # 关闭文件也释放 flock；逐条 flush 不等于断电持久性保证。
        self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def read_session(directory: Path, session_id: str) -> dict[str, Any]:
    """只读详情，不创建模型连接；写入会话尚未关闭时明确报告忙碌。"""
    with session_path(directory, session_id).open(encoding="utf-8") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("会话正在写入，请在任务结束后查询。") from exc
        header, records = _read(file)
        _history(records)
    return {**header.model_dump(), "records": [record.model_dump() for record in records]}


def list_sessions(directory: Path) -> list[dict[str, Any]]:
    """列表只读取不可变文件头与文件更新时间，不加载全部消息正文。"""
    sessions = []
    for path in directory.glob("*.jsonl"):
        with path.open(encoding="utf-8") as file:
            header = _header(file)
        sessions.append(
            {
                **header.model_dump(),
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            }
        )
    return sorted(sessions, key=lambda item: item["updated_at"], reverse=True)
