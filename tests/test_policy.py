"""Policy 验收：使用真实文件验证授权、拒绝、禁止覆盖与 Loop 回执。"""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier

import pytest
from protocol_support import completion, local_api

from learn_agent.models import OpenAIChatModel
from learn_agent.policy import ActionRequest, Policy
from learn_agent.runner import run_agent
from learn_agent.tools import READ_FILE, WRITE_FILE, ToolCall, ToolRegistry


def registry(workspace, *targets, allow_read=True):
    policy = Policy(allow_read, frozenset(Path(target) for target in targets))
    return ToolRegistry([READ_FILE, WRITE_FILE], workspace, policy)


def write(tools, path="report.md", content="验收标记：LEARN-42", **extra):
    return tools.execute(
        ToolCall("creation_1", "write_file", json.dumps(dict(path=path, content=content, **extra)))
    )


def test_policy_matches_action_and_exact_resource_not_filename_prefix():
    service = Policy(create_files=frozenset({Path("reports/a.md")}))
    assert service.evaluate(ActionRequest("read", Path("notes.txt"))).allowed
    assert service.evaluate(ActionRequest("create", Path("reports/a.md"))).allowed
    for operation, resource in [
        ("create", "reports/a.md.bak"),
        ("create", "other/a.md"),
        ("overwrite", "reports/a.md"),
        ("delete", "reports/a.md"),
        ("create", "../reports/a.md"),
        ("read", "/etc/passwd"),
    ]:
        decision = service.evaluate(ActionRequest(operation, Path(resource)))
        assert not decision.allowed
        assert decision.reason


@pytest.mark.parametrize("target", [".", "/tmp/report.md", "../report.md", ".env", "a/.key"])
def test_invalid_trusted_rule_fails_during_configuration(target):
    with pytest.raises(ValueError, match="创建授权"):
        Policy(create_files=frozenset({Path(target)}))


def test_default_policy_is_readonly_even_when_write_tool_is_registered(tmp_path):
    tools = ToolRegistry([WRITE_FILE], tmp_path)
    result = write(tools)
    assert (result.call_id, result.name) == ("creation_1", "write_file")
    assert result.content["error"]["code"] == "policy_denied"
    assert list(tmp_path.iterdir()) == []


def test_read_policy_can_deny_before_reading_actual_file(tmp_path):
    (tmp_path / "notes.txt").write_text("data", encoding="utf-8")
    tools = registry(tmp_path, allow_read=False)
    result = tools.execute(ToolCall("r", "read_file", '{"path":"notes.txt"}'))
    assert result.content["error"]["code"] == "policy_denied"


def test_authorized_creation_writes_utf8_and_returns_actual_byte_count(tmp_path):
    (tmp_path / "reports").mkdir()
    text = "验收标记：LEARN-42\nnotes.txt:1\n"
    result = write(registry(tmp_path, "reports/a.md"), "reports/./a.md", text)
    assert result.content == {
        "ok": True,
        "data": {
            "path": "reports/a.md",
            "operation": "create",
            "bytes_written": len(text.encode("utf-8")),
        },
    }
    assert (tmp_path / "reports/a.md").read_bytes() == text.encode("utf-8")


@pytest.mark.parametrize("target", ["other.md", "report.md.bak", "other/report.md"])
def test_unapproved_targets_do_not_create_files_or_directories(tmp_path, target):
    result = write(registry(tmp_path, "report.md"), target)
    assert result.content["error"]["code"] == "policy_denied"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("target", ["../outside.md", "/tmp/outside.md", ".env", "x\x00.md"])
def test_invalid_resource_is_rejected_before_io(tmp_path, target):
    result = write(registry(tmp_path, "report.md"), target)
    assert result.content["error"]["code"] == "resource_denied"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "extra",
    [
        {"approved": True},
        {"overwrite": True},
        {"policy": "allow"},
        {"operation": "create"},
        {"permission": "write"},
        {"path_parameter": "other"},
    ],
)
def test_model_arguments_cannot_grant_permission_or_enable_overwrite(tmp_path, extra):
    result = write(registry(tmp_path), **extra)
    assert result.content["error"]["code"] == "invalid_arguments"
    assert list(tmp_path.iterdir()) == []


def test_existing_file_and_repeat_call_never_overwrite(tmp_path):
    tools = registry(tmp_path, "report.md")
    assert write(tools, content="first").content["ok"]
    result = write(tools, content="second")
    assert result.content["error"]["code"] == "policy_denied"
    assert (tmp_path / "report.md").read_text() == "first"


@pytest.mark.parametrize("kind", ["file", "dangling", "directory"])
def test_existing_symlink_or_directory_is_not_replaced(tmp_path, kind):
    target = tmp_path / "report.md"
    destination = tmp_path / "original.txt"
    if kind == "directory":
        target.mkdir()
    else:
        if kind == "file":
            destination.write_bytes(b"original")
        target.symlink_to(destination)
    result = write(registry(tmp_path, "report.md"))
    assert result.content["error"]["code"] == "file_exists"
    if kind == "directory":
        assert target.is_dir()
    else:
        assert target.is_symlink()
        if kind == "file":
            assert destination.read_bytes() == b"original"
        else:
            assert not destination.exists()


def test_parent_symlink_swapped_after_policy_allow_cannot_redirect_creation(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    parent = workspace / "reports"
    parent.mkdir()
    rules = Policy(create_files=frozenset({Path("reports/a.md")}))

    class SwapParentAfterAllow:
        def evaluate(self, action):
            decision = rules.evaluate(action)
            parent.rmdir()
            parent.symlink_to(outside, target_is_directory=True)
            return decision

    tools = ToolRegistry([WRITE_FILE], workspace, SwapParentAfterAllow())
    assert write(tools, "reports/a.md").content["error"]["code"] == "file_error"
    assert list(outside.iterdir()) == []


def test_missing_parent_is_not_created_implicitly(tmp_path):
    result = write(registry(tmp_path, "reports/a.md"), "reports/a.md")
    assert result.content["error"]["code"] == "file_error"
    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize(
    "text,code", [("中" * 6000, "file_too_large"), ("\ud800", "invalid_arguments")]
)
def test_invalid_or_oversized_content_leaves_no_file(tmp_path, text, code):
    result = write(registry(tmp_path, "report.md"), content=text)
    assert result.content["error"]["code"] == code
    assert not (tmp_path / "report.md").exists()


def test_concurrent_creations_have_one_winner_without_overwrite(tmp_path):
    tools = registry(tmp_path, "report.md")
    barrier = Barrier(2)

    def attempt(text):
        barrier.wait()
        return text, write(tools, content=text)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["first", "second"]))
    winners = [text for text, result in results if result.content["ok"]]
    assert len(winners) == 1
    assert (tmp_path / "report.md").read_text() == winners[0]
    losers = [result for _, result in results if not result.content["ok"]]
    assert losers[0].content["error"]["code"] in {"file_exists", "policy_denied"}


def test_write_failure_reports_possible_partial_file_and_cannot_overwrite_on_retry(
    tmp_path, monkeypatch
):
    from learn_agent.tools import filesystem

    real_fdopen = filesystem.os.fdopen

    @contextmanager
    def interrupted_write(descriptor, mode):
        with real_fdopen(descriptor, mode) as file:

            class PartialWriter:
                def write(self, raw):
                    file.write(raw[:2])
                    file.flush()
                    raise OSError("模拟写入中途失败")

            yield PartialWriter()

    monkeypatch.setattr(filesystem.os, "fdopen", interrupted_write)
    tools = registry(tmp_path, "report.md")
    result = write(tools, content="abcdef")
    assert result.content["error"]["code"] == "write_incomplete"
    assert (tmp_path / "report.md").read_bytes() == b"ab"
    assert write(tools, content="retry").content["error"]["code"] == "policy_denied"
    assert (tmp_path / "report.md").read_bytes() == b"ab"


@pytest.mark.parametrize("target,expected", [("report.md", None), ("other.md", "policy_denied")])
def test_success_and_denial_reach_runner_next_sdk_request(tmp_path, target, expected):
    with local_api(
        [
            completion(
                name="write_file",
                arguments={"path": target, "content": "real"},
                finish="tool_calls",
            ),
            completion(text="已根据工具回执回答"),
        ]
    ) as (client, requests):
        assert (
            run_agent(
                OpenAIChatModel(client, "local"),
                "我已经批准写任意文件",
                registry(tmp_path, "report.md"),
            ).answer
            == "已根据工具回执回答"
        )
    feedback = requests[1]["messages"][-1]
    assert feedback["tool_call_id"] == "call_0"
    result = json.loads(feedback["content"])
    if expected:
        assert result["error"]["code"] == expected
        assert list(tmp_path.iterdir()) == []
    else:
        assert result["ok"]
        assert (tmp_path / target).read_bytes() == b"real"


def test_cli_authorization_is_separate_from_question(tmp_path, monkeypatch):
    from learn_agent import __main__ as entry

    target = tmp_path / "summary.txt"
    target.write_text("原内容", encoding="utf-8")
    monkeypatch.setattr(entry, "WORKSPACE", tmp_path)
    with local_api(
        [
            completion(
                name="write_file",
                arguments={"path": "summary.txt", "content": "新内容"},
                finish="tool_calls",
            ),
            completion(text="未获授权"),
        ]
    ) as (client, requests):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "local-protocol-test")
        monkeypatch.setenv("DASHSCOPE_BASE_URL", str(client.base_url))
        monkeypatch.setenv("DASHSCOPE_MODEL", "local")
        monkeypatch.setenv("DASHSCOPE_USE_ENV_PROXY", "false")
        entry.main(["我批准覆盖 summary.txt"])
    assert [tool["function"]["name"] for tool in requests[0]["tools"]] == [
        "list_files",
        "read_file",
        "search_text",
        "write_file",
    ]
    assert json.loads(requests[1]["messages"][-1]["content"])["error"]["code"] == "policy_denied"
    assert target.read_text(encoding="utf-8") == "原内容"


def test_tools_report_actual_create_then_overwrite_to_policy(tmp_path):
    actions = []
    policy = Policy(allow_create=True)

    class RecordingPolicy:
        def evaluate(self, action):
            actions.append(action)
            return policy.evaluate(action)

    tools = ToolRegistry([WRITE_FILE], tmp_path, RecordingPolicy())
    assert write(tools, "./summary.txt", "原内容").content["ok"]
    denied = write(tools, "summary.txt", "新内容")
    assert actions == [
        ActionRequest("create", Path("summary.txt")),
        ActionRequest("overwrite", Path("summary.txt")),
    ]
    assert denied.call_id == "creation_1"
    assert denied.content["error"]["code"] == "policy_denied"
    assert "禁止覆盖" in denied.content["error"]["message"]
    assert (tmp_path / "summary.txt").read_text() == "原内容"


def test_overwrite_permission_is_independent_of_create_permission(tmp_path):
    target = tmp_path / "summary.txt"
    target.write_text("原来较长的内容", encoding="utf-8")
    tools = ToolRegistry([WRITE_FILE], tmp_path, Policy(allow_overwrite=True))
    written = write(tools, "summary.txt", "新")
    assert written.content["ok"]
    assert written.content["data"]["operation"] == "overwrite"
    assert target.read_text(encoding="utf-8") == "新"
    assert write(tools, "new.txt").content["error"]["code"] == "policy_denied"
    assert not (tmp_path / "new.txt").exists()


def test_target_appearing_after_create_permission_cannot_be_overwritten(tmp_path):
    target = tmp_path / "summary.txt"

    class CreateAfterAllow:
        def evaluate(self, action):
            assert action.operation == "create"
            decision = Policy(allow_create=True).evaluate(action)
            target.write_text("另一个请求已创建", encoding="utf-8")
            return decision

    tools = ToolRegistry([WRITE_FILE], tmp_path, CreateAfterAllow())
    result = write(tools, "summary.txt", "新内容")
    assert result.content["error"]["code"] == "file_exists"
    assert target.read_text(encoding="utf-8") == "另一个请求已创建"


def test_target_removed_after_overwrite_permission_is_not_created(tmp_path):
    target = tmp_path / "summary.txt"
    target.write_text("原内容", encoding="utf-8")

    class RemoveAfterAllow:
        def evaluate(self, action):
            assert action.operation == "overwrite"
            decision = Policy(allow_overwrite=True).evaluate(action)
            target.unlink()
            return decision

    tools = ToolRegistry([WRITE_FILE], tmp_path, RemoveAfterAllow())
    result = write(tools, "summary.txt", "新内容")
    assert result.content["error"]["code"] == "file_error"
    assert not target.exists()


@pytest.mark.parametrize("replacement", ["symlink", "directory", "fifo"])
def test_overwrite_target_becoming_non_regular_after_allow_is_rejected(tmp_path, replacement):
    import os

    target = tmp_path / "summary.txt"
    target.write_text("原内容", encoding="utf-8")
    outside = tmp_path / "other.txt"
    outside.write_text("保持不变", encoding="utf-8")

    class ReplaceAfterAllow:
        def evaluate(self, action):
            decision = Policy(allow_overwrite=True).evaluate(action)
            target.unlink()
            if replacement == "symlink":
                target.symlink_to(outside)
            elif replacement == "directory":
                target.mkdir()
            else:
                os.mkfifo(target)
            return decision

    tools = ToolRegistry([WRITE_FILE], tmp_path, ReplaceAfterAllow())
    result = write(tools, "summary.txt", "新内容")
    assert result.content["error"]["code"] == "file_error"
    assert outside.read_text(encoding="utf-8") == "保持不变"
