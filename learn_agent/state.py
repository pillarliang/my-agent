"""阶段 3A：用 SQLite 保存会话与执行记录，供后续追问和查询。

入口创建 SessionStore 并管理连接；Runner 调用保存方法推进一次任务：
start_run → start_step → append_message（模型响应、工具回执）→ finish_run。
一次任务可以有多个 Step，每个 Step 对应一次模型请求及其工具回执。

数据分两层：表的普通列保存 ID、步号、状态等；TEXT 列中的 JSON 保存复杂内容。
history 将消息 JSON 还原为模型接口使用的 Python 对象；inspect_session 将各表的
JSON 解析成普通字典供人查看。保存历史不代表能自动恢复或重做中断任务。
"""

import json
import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from uuid import uuid4

from pydantic import TypeAdapter

from .models import ModelMessage, ModelRequest, TextMessage

# ModelMessage 是 type 定义的联合类型别名：TextMessage | ModelResponse | ToolResult。
# __value__ 取得其实际类型表达式；TypeAdapter 根据这些类型负责序列化和还原，
# 不要求业务数据类继承 Pydantic BaseModel，也不为每种消息另写一套转换函数。
# dump_json 返回 UTF-8 bytes，写入 SQLite TEXT 前用 decode 转为 str；
# validate_json 则将 JSON 文本还原为对应的数据类，连嵌套的 ToolCall 等也一并还原。
MESSAGE = TypeAdapter[ModelMessage](ModelMessage.__value__)
# ModelRequest 含 messages、tools、max_output_tokens，保存的是完整模型输入快照。
REQUEST = TypeAdapter(ModelRequest)


class SessionStore:
    """管理一个 SQLite 连接，并以短事务逐步保存记录。

    每次保存方法中的 with self._db 管理事务：正常退出时提交，异常时回滚。
    它不关闭连接；with SessionStore(...) 才会在退出时调用本类的 close。
    整个 Agent 任务不共用一个长事务，因此模型调用期间不持续占用写事务，
    已提交的消息也不会因后续模型失败而全部丢失。
    """

    def __init__(self, path: Path) -> None:
        # path 是数据库文件路径，不是工具工作目录；不存在时创建父目录和数据库。
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        # 查询结果支持 row["字段名"] 和 dict(row)，避免靠列的位置取值。
        self._db.row_factory = sqlite3.Row
        # 外键检查按连接开启，保证子记录引用的 Session / Run 必须存在。
        self._db.execute("PRAGMA foreign_keys = ON")
        # 五张业务表的列与数据含义（完整示例见 docs/stages/stage-3a.html 的 3.4）：
        # sessions：id 为会话主键；workspace 为工作目录绝对路径；created_at 为创建时间。
        # runs：sequence 为排序用的自增主键；id 为对外使用的唯一 Run ID；
        #   session_id 引用 sessions.id；status 只能是 running/completed/failed；
        #   result 存 {answer, tool_results, session_id, run_id} 的 JSON 文本，
        #   error 存 {code, message} 的 JSON 文本；二者尚未写入时为 SQL NULL。
        #   created_at 为开始时间，finished_at 为结束时间，未结束时后者为 NULL。
        # steps：run_id 引用 runs.id；number 为 Run 内从 1 开始的模型请求序号；
        #   (run_id, number) 是联合主键；request 为完整 ModelRequest 的 JSON 文本；
        #   created_at 为保存请求的时间，而非模型实际返回的时间。
        # messages：sequence 为消息排序用的自增主键；run_id 引用 runs.id；
        #   step 为产生消息的步号，用户最初的问题记为 0；message 保存一条消息的 JSON。
        # events：sequence 为事件排序用的自增主键；run_id 引用 runs.id；
        #   step 为步号，运行开始和结束记为 0；kind 表示事件类型；
        #   data 为补充数据 JSON，通常为 {}，失败结束时保存错误；created_at 为记录时间。
        #
        # 自增序号用于稳定排序，UUID 用于标识会话和任务；UUID 字典序不代表发生顺序。
        # messages/events 的 step 没有到 steps 的外键，因此允许没有对应 Step 的 0。
        # 不同表的 sequence 独立增长，不能用它们相等来关联消息和事件。
        # 所有时间由 SQLite 写成 UTC 文本；JSON 也存 TEXT，不是独立的子表。
        # one_active_run 是部分唯一索引：只对 running 行限制 session_id 唯一，
        # 已完成/失败任务不受这个唯一性限制，同一会话可以保留多次历史任务。
        # IF NOT EXISTS 支持重新打开已有数据库，但不会迁移已有表的字段结构。
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE IF NOT EXISTS runs (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL REFERENCES sessions(id),
                status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
                result TEXT, error TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                finished_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_run
                ON runs(session_id) WHERE status = 'running';
            CREATE TABLE IF NOT EXISTS steps (
                run_id TEXT NOT NULL REFERENCES runs(id), number INTEGER NOT NULL,
                request TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                PRIMARY KEY (run_id, number)
            );
            CREATE TABLE IF NOT EXISTS messages (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id), step INTEGER NOT NULL,
                message TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id), step INTEGER NOT NULL,
                kind TEXT NOT NULL, data TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
        """)

    def close(self) -> None:
        """释放数据库连接；各保存方法已负责提交自己的事务。"""
        self._db.close()

    def __enter__(self) -> Self:
        # with SessionStore(path) as store 中的 store 就是当前实例；Self 表示本类类型。
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # 即使 with 内发生异常也关闭连接；不返回 True，因此不会吞掉原异常。
        self.close()

    def create_session(self, workspace: Path) -> str:
        """新增会话并返回 ID；此时还没有用户任务，也不创建 Run。

        workspace.resolve() 统一保存绝对路径，后续复用会话时据此检查工作目录。
        同一工作目录可以有多个会话；会话 ID 是 32 位十六进制 UUID 字符串。
        """
        session_id = uuid4().hex
        with self._db:
            self._db.execute(
                "INSERT INTO sessions (id, workspace) VALUES (?, ?)",
                (session_id, str(workspace.resolve())),
            )
        return session_id

    def check_workspace(self, session_id: str, workspace: Path) -> None:
        """应用在启动任务前核对目录，避免把另一目录的历史当成本次文件状态。"""
        row = self._session(session_id)
        if row["workspace"] != str(workspace.resolve()):
            raise ValueError("会话的工作目录与当前配置不一致，请创建新会话。")

    def _session(self, session_id: str) -> sqlite3.Row:
        """读取一行会话元数据；未知 ID 明确报错，不自动新建。"""
        # ? 是绑定参数占位符，值与 SQL 分开传递；(session_id,) 是单元素元组。
        # 主键最多命中一行，fetchone 无结果时返回 None。
        row: sqlite3.Row | None = self._db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"会话不存在：{session_id}")
        return row

    def _event(self, run_id: str, step: int, kind: str, data: dict[str, Any]) -> None:
        """在调用方的事务中追加事件，不独立提交。

        调用方先写消息/请求/状态，再调用本方法；任意一项失败都会一起回滚。
        ensure_ascii=False 保留可读的中文；json.dumps 的结果仍是 JSON 字符串。
        """
        self._db.execute(
            "INSERT INTO events (run_id, step, kind, data) VALUES (?, ?, ?, ?)",
            (run_id, step, kind, json.dumps(data, ensure_ascii=False)),
        )

    def start_run(self, session_id: str, question: str) -> str:
        """创建 Run、保存用户问题和开始事件，一起提交后返回 Run ID。

        一次调用写 runs、messages、events 三张表；question 在 messages 中保存，
        runs 本身没有 question 列。同一会话即使是追问，也要创建新的 Run。
        """
        run_id = uuid4().hex
        with self._db:
            # 先取得写锁再检查，防止两个进程同时通过“没有正在运行的任务”的判断。
            # BEGIN IMMEDIATE 显式开始写事务；后续发现冲突抛出异常时由 with 回滚。
            self._db.execute("BEGIN IMMEDIATE")
            self._session(session_id)
            active = self._db.execute(
                "SELECT id FROM runs WHERE session_id = ? AND status = 'running'", (session_id,)
            ).fetchone()
            if active is not None:
                # running 既可能是另一个正在执行的任务，也可能是崩溃后遗留的任务。
                # 无法仅凭记录确定工具是否已执行，因此不擅自结束它或重新执行工具。
                raise ValueError(
                    f"会话有未结束的运行 {active['id']}；请查询记录并核对工作区。"
                    "当前阶段不自动恢复，请使用新会话提交新任务。"
                )
            self._db.execute(
                "INSERT INTO runs (id, session_id, status) VALUES (?, ?, 'running')",
                (run_id, session_id),
            )
            # 此时尚未请求模型，所以用户问题属于运行级消息，步号为 0。
            # 例如 TextMessage("user", "读取 notes.txt") 被写成
            # {"role":"user","content":"读取 notes.txt"} 这段 JSON 文本。
            self._db.execute(
                "INSERT INTO messages (run_id, step, message) VALUES (?, 0, ?)",
                (run_id, MESSAGE.dump_json(TextMessage("user", question)).decode()),
            )
            self._event(run_id, 0, "run_started", {})
        return run_id

    def history(self, session_id: str) -> tuple[ModelMessage, ...]:
        """返回同一会话已完成任务的消息对象元组，供 Runner 组成下一次模型输入。

        只读 messages.message，不读取 runs.result 或旧 steps.request 来拼历史。
        失败与中断任务可能缺少完整工具回执，因此仅供 inspect_session 查询。
        """
        self._session(session_id)
        # messages 没有 session_id，需通过 JOIN runs 找到消息所属的会话和任务状态。
        # Runner 已新建的当前 Run 仍为 running，其用户问题不会被这里重复取出。
        # 先按任务顺序、再按消息顺序排列，保留“调用 → 工具回执 → 回答”的关系。
        rows = self._db.execute(
            "SELECT messages.message FROM messages JOIN runs ON messages.run_id = runs.id "
            "WHERE runs.session_id = ? AND runs.status = 'completed' "
            "ORDER BY runs.sequence, messages.sequence",
            (session_id,),
        ).fetchall()
        # 还原后是 TextMessage / ModelResponse / ToolResult 实例，而非数据库行或字典。
        # 例如工具消息可用 history[2].content["data"]["text"] 访问正文。
        # 没有已完成任务时返回空元组；当前读取全部历史，不裁剪、不重放工具调用。
        return tuple(MESSAGE.validate_json(row["message"]) for row in rows)

    def start_step(self, run_id: str, number: int, request: ModelRequest) -> None:
        """请求模型前保存完整 ModelRequest，不包含凭证或 SDK client。"""
        # request JSON 包含 messages（含 SYSTEM）、tools（工具声明）和 max_output_tokens。
        # 保存完整快照会重复包含先前消息，换取能够直接核对每一步模型输入。
        # 先序列化再进入事务，转换失败时不会写出半条步骤记录。
        payload = REQUEST.dump_json(request).decode()
        with self._db:
            self._db.execute(
                "INSERT INTO steps (run_id, number, request) VALUES (?, ?, ?)",
                (run_id, number, payload),
            )
            # 此事件只证明准备发送的请求已保存，不证明模型服务已经收到请求。
            self._event(run_id, number, "model_requested", {})

    def append_message(self, run_id: str, step: int, message: ModelMessage, kind: str) -> None:
        """保存一条消息及对应事件；工具结果必须在工具实际返回后写入。

        Runner 传入 ModelResponse 时 kind 为 model_responded，传入 ToolResult 时
        为 tool_returned。一个响应中的多个 tool_calls 随响应 JSON 保存，不另建行；
        每个工具返回的 ToolResult 则各占一行，并用 call_id 对应到原来的调用。
        """
        # message JSON 随类型变化：用户为 {role, content}；工具为 {call_id, name, content}；
        # 模型为 {text, tool_calls, stop_reason, model, usage, refusal, continuation}。
        # 模型的用量和原生续接材料也会保存，不只是保存展示给用户的文字。
        # 数据库事务无法包住外部文件操作：文件可能已修改，但回执尚未来得及保存。
        with self._db:
            self._db.execute(
                "INSERT INTO messages (run_id, step, message) VALUES (?, ?, ?)",
                (run_id, step, MESSAGE.dump_json(message).decode()),
            )
            self._event(run_id, step, kind, {})

    def finish_run(
        self,
        run_id: str,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, str] | None = None,
    ) -> None:
        """一次性保存终态、结果或错误、结束时间和结束事件。

        result 来自 Runner 的 asdict(RunResult)，包含回答、工具回执和会话/运行 ID；
        error 为 {code, message}。这里接收的是普通字典，因此直接用 json.dumps。
        只能提供其中一个；完成记录用于查询，不代表对实际任务成果做了独立验收。
        """
        # 两个 is None 的结果相等，表示“都没提供”或“同时提供”，这两种都不允许。
        if (result is None) == (error is None):
            raise ValueError("结束运行必须且只能提供结果或错误。")
        status = "completed" if result is not None else "failed"
        with self._db:
            # WHERE 同时检查 ID 和 running 状态，防止重复结束或覆盖已有终态。
            # Python None 绑定为 SQL NULL；提供的字典则序列化成 JSON 文本。
            changed = self._db.execute(
                "UPDATE runs SET status = ?, result = ?, error = ?, "
                "finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE id = ? AND status = 'running'",
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    json.dumps(error, ensure_ascii=False) if error is not None else None,
                    run_id,
                ),
            )
            if changed.rowcount != 1:
                # ID 不存在或任务已经结束都会更新 0 行；异常触发当前事务回滚。
                raise ValueError("只能结束正在运行的 Run。")
            # 结束事件属于整个 Run，步号用 0；失败事件附带错误，成功事件为 {}。
            # 如果事件插入失败，上面的状态、结果和结束时间更新也一起回滚。
            self._event(run_id, 0, f"run_{status}", error or {})

    def inspect_session(self, session_id: str) -> dict[str, Any]:
        """查询五张表并组装普通字典，供 --show-session 等查询入口展示。

        返回形状为会话字段加 runs 列表；每个 run 保留自身字段，并包含
        messages、steps、events 三个列表。列表里的元素保留各自数据库行的列，
        其中 message/request/data 和 result/error 的 JSON 文本被解析成字典。
        与 history 不同，这里包含所有任务状态，也不还原为模型数据类。
        """
        session = dict(self._session(session_id))
        runs = []
        for row in self._db.execute(
            "SELECT * FROM runs WHERE session_id = ? ORDER BY sequence", (session_id,)
        ).fetchall():
            run = dict(row)
            for field in ("result", "error"):
                # 尚未写入的 SQL NULL 保持为 None；不能把它当作 JSON 字符串解析。
                run[field] = json.loads(run[field]) if run[field] is not None else None
            # 每项分别指定：查询哪张子表、解析哪个 JSON 列、按哪个字段排序。
            # messages 按产生顺序，steps 按模型请求步号，events 按记录顺序展示。
            for table, field, order in (
                ("messages", "message", "sequence"),
                ("steps", "request", "number"),
                ("events", "data", "sequence"),
            ):
                entries = []
                # 表名/列名不能用 ? 绑定；这里只拼接上方固定常量，不接收用户表名。
                # 外部输入仍只通过绑定参数进入查询。
                for item in self._db.execute(
                    f"SELECT * FROM {table} WHERE run_id = ? ORDER BY {order}", (run["id"],)
                ):
                    entry = dict(item)
                    entry[field] = json.loads(entry[field])
                    entries.append(entry)
                # 这里只为查询结果增加嵌套列表，不是在 SQLite 中新增列或重复写入数据。
                run[table] = entries
            runs.append(run)
        session["runs"] = runs
        return session
