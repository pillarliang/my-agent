"""Shared assertions over actual model and tool execution records."""

from learn_agent.application import AgentService

REPORT_TASK = (
    "请先使用 read_file 读取 notes.txt，再使用 create_file 新建 report.md。"
    "报告必须包含来源文件中的验收标记。"
)
READ_TASK = "请务必使用 read_file 读取 notes.txt，回答验收标记并说明来源文件。"


def successful_tools(service: AgentService, run_id: str) -> list[str]:
    return [
        event["data"]["tool"]
        for event in service.inspect(run_id)["events"]
        if event["name"] == "tool.finished" and event["data"]["ok"]
    ]
