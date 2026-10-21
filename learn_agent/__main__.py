"""应用入口：默认读取；显式指定 --create-file 才授权创建该文件。"""

import argparse
from collections.abc import Sequence
from pathlib import Path

from .models import LLMService
from .policy import Policy, PolicyService
from .runner import run_agent
from .tools import CREATE_FILE, READ_FILE, SEARCH_TEXT, ToolContext, ToolRegistry
from .verifier import ReportVerifier, VerificationRequest, VerificationSpec, Verifier

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "examples/workspace"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 参数是可信应用配置；question 只是任务文本，不能授予权限。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=WORKSPACE, help="文件工具工作区")
    parser.add_argument("--create-file", help="阶段 2：只允许新建此相对路径，禁止覆盖")
    parser.add_argument("--question", help="自定义任务文本，不改变创建授权")
    options = parser.parse_args(argv)
    targets = (
        frozenset({Path(options.create_file)}) if options.create_file is not None else frozenset()
    )
    try:
        policy = PolicyService(Policy(create_files=targets))
        spec = VerificationSpec(Path(options.create_file)) if targets else None
    except ValueError as exc:
        parser.error(str(exc))
    context = ToolContext(options.workspace, policy)
    tools = ToolRegistry(
        [READ_FILE, SEARCH_TEXT, CREATE_FILE] if targets else [READ_FILE, SEARCH_TEXT],
        context,
    )
    question = "请用 search_text 在 notes.txt 中查找‘验收标记’，回答标记内容并给出路径和行号。"
    if targets:
        question += (
            f"然后用 create_file 新建 {options.create_file!r}，正文严格三行："
            "第一行是 # 验收报告；第二行原样复制找到的验收标记整行；"
            "第三行是 来源：notes.txt:实际行号。不要添加代码围栏或其他说明。"
            "若工具失败，请如实说明，不要换路径或声称成功。"
        )
    question = options.question if options.question is not None else question
    model = LLMService()
    try:
        run = run_agent(model, question, tools)
    finally:
        model.close()
    print(f"\n模型说明：\n{run.answer}")
    verifier: Verifier = ReportVerifier(context)
    verification = verifier.verify(VerificationRequest(spec, run.tool_results))
    print(f"\n产物验收：{verification.status}")
    for check in verification.checks:
        label = "通过" if check.passed else "失败"
        print(f"  {label} {check.name}：{check.evidence}")
    if verification.status == "not_checked":
        print("  未配置产物验收，不能据此判断任务通过。")
    return 1 if verification.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
