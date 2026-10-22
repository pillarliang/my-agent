"""通用命令行入口：接收任务、设置工作区与权限、运行并显示结果。"""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .application import run_task
from .policy import Policy
from .state import SessionStore

WORKSPACE = Path(__file__).resolve().parents[1] / "examples/workspace"
DATABASE = Path(__file__).resolve().parents[1] / ".learn-agent/sessions.sqlite3"


def main(argv: Sequence[str] | None = None) -> int:
    """接收任务、提供工作目录与权限，再运行并显示模型回答。"""
    parser = argparse.ArgumentParser(description=__doc__)
    # question 是位置参数，直接写任务文本，不用加 --question。
    # nargs="?" 表示接收零个或一个值；省略时为 None，后面通过 input() 询问。
    parser.add_argument("question", nargs="?", help="自然语言任务；省略时在终端输入")
    # 下面两个参数互斥：一次启动只能选择“提交新任务”或“查询记录”，也可以都不传。
    action = parser.add_mutually_exclusive_group()
    # --session 后接已有会话 ID；不传时，执行任务前会创建新会话。
    action.add_argument("--session", help="在已有会话中提交一个新任务")
    # --show-session 后接要查询的会话 ID，只展示记录，不启动新的模型任务。
    action.add_argument("--show-session", help="查询完整记录，不请求模型")
    # help 文本通过 --help 查看；解析结果可用 options.question、options.session 等访问。
    options = parser.parse_args(argv)
    if options.show_session:
        # 查询走独立的只读流程，不创建模型服务，也不执行工具。
        if options.question is not None:
            parser.error("查询记录时不接收新任务。")
        with SessionStore(DATABASE) as store:
            print(
                json.dumps(
                    store.inspect_session(options.show_session), ensure_ascii=False, indent=2
                )
            )
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
    # 入口管理存储连接；新会话在这里创建，已有会话由应用核对工作目录。
    with SessionStore(DATABASE) as store:
        session_id = options.session or store.create_session(WORKSPACE)
        print(f"会话：{session_id}")
        print(f"记录：{DATABASE}")
        run = run_task(
            question,
            workspace=WORKSPACE,
            policy=policy,
            store=store,
            session_id=session_id,
        )
    print(f"运行：{run.run_id}")
    print(f"\n模型说明：\n{run.answer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
