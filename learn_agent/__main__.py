"""通用命令行入口：执行任务、在同一会话追问或查询本地记录。"""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .application import run_task
from .policy import Policy
from .session import SessionLog, list_sessions, read_session

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "examples/workspace"
SESSIONS_DIRECTORY = ROOT / ".learn-agent/sessions"


def main(argv: Sequence[str] | None = None) -> int:
    """查询不创建模型；执行时先打开会话，再提供工作区与权限。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?", help="自然语言任务；省略时在终端输入")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--session", metavar="ID", help="在已有会话中开始新任务")
    mode.add_argument("--show-session", metavar="ID", help="查询会话完整记录，不调用模型")
    mode.add_argument("--list-sessions", action="store_true", help="列出本地会话，不调用模型")
    options = parser.parse_args(argv)
    if options.show_session is not None or options.list_sessions:
        if options.question is not None:
            parser.error("查询记录时不能同时提供任务。")
        data = (
            read_session(SESSIONS_DIRECTORY, options.show_session)
            if options.show_session is not None
            else list_sessions(SESSIONS_DIRECTORY)
        )
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    print(f"工作目录：{WORKSPACE}")
    try:
        question = options.question if options.question is not None else input("请输入任务：")
    except EOFError:
        parser.error("没有读到任务，请在终端输入或通过命令行提供任务文本。")
    question = question.strip()
    if not question:
        parser.error("任务不能为空。")

    policy = Policy(allow_create=True)
    with SessionLog(SESSIONS_DIRECTORY, session_id=options.session, workspace=WORKSPACE) as session:
        # 执行前输出 ID；即使模型失败或进程中断，仍可定位已有记录。
        print(f"会话 ID：{session.header.session_id}", flush=True)
        run = run_task(question, workspace=WORKSPACE, policy=policy, session=session)
        print(f"任务 ID：{run.turn_id}")
        print(f"\n模型说明：\n{run.answer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
