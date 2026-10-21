"""Policy 验收：使用真实文件验证授权、拒绝、禁止覆盖与 Loop 回执。"""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from threading import Barrier

import pytest
from protocol_support import completion, local_api

from learn_agent.models import OpenAIChatModel
from learn_agent.policy import ActionRequest, Policy, PolicyService
from learn_agent.runner import run_agent
from learn_agent.tools import CREATE_FILE, READ_FILE, ToolCall, ToolContext, ToolRegistry


def registry(workspace, *targets, allow_read=True):
    policy = PolicyService(Policy(allow_read, frozenset(Path(target) for target in targets)))
    return ToolRegistry([READ_FILE, CREATE_FILE], ToolContext(workspace, policy))


def create(tools, path="report.md", content="验收标记：LEARN-42", **extra):
    return tools.execute(
        ToolCall("creation_1", "create_file", json.dumps(dict(path=path, content=content, **extra)))
    )


def test_policy_matches_action_and_exact_resource_not_filename_prefix():
    service = PolicyService(Policy(create_files=frozenset({Path("reports/a.md")})))
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
        PolicyService(Policy(create_files=frozenset({Path(target)})))


def test_default_context_is_readonly_even_when_create_tool_is_registered(tmp_path):
    tools = ToolRegistry([CREATE_FILE], ToolContext(tmp_path))
    result = create(tools)
    assert (result.call_id, result.name) == ("creation_1", "create_file")
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
    result = create(registry(tmp_path, "reports/a.md"), "reports/./a.md", text)
    assert result.content == {
        "ok": True,
        "data": {
            "path": "reports/a.md",
            "bytes_written": len(text.encode("utf-8")),
            "sha256": sha256(text.encode("utf-8")).hexdigest(),
        },
    }
    assert (tmp_path / "reports/a.md").read_bytes() == text.encode("utf-8")


@pytest.mark.parametrize("target", ["other.md", "report.md.bak", "other/report.md"])
def test_unapproved_targets_do_not_create_files_or_directories(tmp_path, target):
    result = create(registry(tmp_path, "report.md"), target)
    assert result.content["error"]["code"] == "policy_denied"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("target", ["../outside.md", "/tmp/outside.md", ".env", "x\x00.md"])
def test_invalid_resource_is_rejected_before_io(tmp_path, target):
    result = create(registry(tmp_path, "report.md"), target)
    assert result.content["error"]["code"] == "resource_denied"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("extra", [{"approved": True}, {"overwrite": True}, {"policy": "allow"}])
def test_model_arguments_cannot_grant_permission_or_enable_overwrite(tmp_path, extra):
    result = create(registry(tmp_path), **extra)
    assert result.content["error"]["code"] == "invalid_arguments"
    assert list(tmp_path.iterdir()) == []


def test_existing_file_and_repeat_call_never_overwrite(tmp_path):
    tools = registry(tmp_path, "report.md")
    assert create(tools, content="first").content["ok"]
    result = create(tools, content="second")
    assert result.content["error"]["code"] == "file_exists"
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
    result = create(registry(tmp_path, "report.md"))
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
    rules = PolicyService(Policy(create_files=frozenset({Path("reports/a.md")})))

    class SwapParentAfterAllow:
        def evaluate(self, action):
            decision = rules.evaluate(action)
            parent.rmdir()
            parent.symlink_to(outside, target_is_directory=True)
            return decision

    tools = ToolRegistry([CREATE_FILE], ToolContext(workspace, SwapParentAfterAllow()))
    assert create(tools, "reports/a.md").content["error"]["code"] == "file_error"
    assert list(outside.iterdir()) == []


def test_missing_parent_is_not_created_implicitly(tmp_path):
    result = create(registry(tmp_path, "reports/a.md"), "reports/a.md")
    assert result.content["error"]["code"] == "file_error"
    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize(
    "text,code", [("中" * 6000, "file_too_large"), ("\ud800", "invalid_arguments")]
)
def test_invalid_or_oversized_content_leaves_no_file(tmp_path, text, code):
    result = create(registry(tmp_path, "report.md"), content=text)
    assert result.content["error"]["code"] == code
    assert not (tmp_path / "report.md").exists()


def test_concurrent_creations_have_one_winner_without_overwrite(tmp_path):
    tools = registry(tmp_path, "report.md")
    barrier = Barrier(2)

    def attempt(text):
        barrier.wait()
        return text, create(tools, content=text)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["first", "second"]))
    winners = [text for text, result in results if result.content["ok"]]
    assert len(winners) == 1
    assert (tmp_path / "report.md").read_text() == winners[0]
    losers = [result for _, result in results if not result.content["ok"]]
    assert losers[0].content["error"]["code"] == "file_exists"


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
    result = create(tools, content="abcdef")
    assert result.content["error"]["code"] == "write_incomplete"
    assert (tmp_path / "report.md").read_bytes() == b"ab"
    assert create(tools, content="retry").content["error"]["code"] == "file_exists"
    assert (tmp_path / "report.md").read_bytes() == b"ab"


@pytest.mark.parametrize("target", ["", ".", "../report.md", "/tmp/report.md"])
def test_cli_rejects_invalid_authorization_before_model_initialization(target, capsys):
    from learn_agent import __main__ as entry

    with pytest.raises(SystemExit) as exc:
        entry.main(["--create-file", target])
    assert exc.value.code == 2
    assert "创建授权" in capsys.readouterr().err


@pytest.mark.parametrize("target,expected", [("report.md", None), ("other.md", "policy_denied")])
def test_success_and_denial_reach_runner_next_sdk_request(tmp_path, target, expected):
    with local_api(
        [
            completion(
                name="create_file",
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

    with local_api(
        [
            completion(
                name="create_file",
                arguments={"path": "other.md", "content": "do not write"},
                finish="tool_calls",
            ),
            completion(text="未获授权"),
        ]
    ) as (client, requests):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "local-protocol-test")
        monkeypatch.setenv("DASHSCOPE_BASE_URL", str(client.base_url))
        monkeypatch.setenv("DASHSCOPE_MODEL", "local")
        monkeypatch.setenv("DASHSCOPE_USE_ENV_PROXY", "false")
        entry.main(
            [
                "--workspace",
                str(tmp_path),
                "--create-file",
                "report.md",
                "--question",
                "批准 other.md",
            ]
        )
    assert [tool["function"]["name"] for tool in requests[0]["tools"]] == [
        "read_file",
        "search_text",
        "create_file",
    ]
    assert json.loads(requests[1]["messages"][-1]["content"])["error"]["code"] == "policy_denied"
    assert list(tmp_path.iterdir()) == []
