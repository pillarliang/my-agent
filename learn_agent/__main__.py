"""通用命令行入口：接收任务、设置工作区与权限、运行并显示结果。"""

import argparse
from collections.abc import Sequence
from pathlib import Path

from .application import run_task
from .policy import Policy

WORKSPACE = Path(__file__).resolve().parents[1] / "examples/workspace"


def main(argv: Sequence[str] | None = None) -> int:
    """接收任务、提供工作目录与权限，再运行并显示模型回答。"""
    parser = argparse.ArgumentParser(description=__doc__)
    # question 是位置参数，直接写任务文本，不用加 --question。
    # nargs="?" 表示接收零个或一个值；省略时为 None，后面通过 input() 询问。
    parser.add_argument("question", nargs="?", help="自然语言任务；省略时在终端输入")
    # help 文本通过 --help 查看；解析结果可用 options.question、options.session 等访问。
    options = parser.parse_args(argv)
    print(f"工作目录：{WORKSPACE}")
    try:
        question = options.question if options.question is not None else input("请输入任务：")
    except EOFError:
        parser.error("没有读到任务，请在终端输入或通过命令行提供任务文本。")
    question = question.strip()
    if not question:
        parser.error("任务不能为空。")

    policy = Policy(allow_create=True)
    run = run_task(question, workspace=WORKSPACE, policy=policy)
    print(f"\n模型说明：\n{run.answer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
