import json
from pathlib import Path

import pytest

from learn_agent.contracts import ToolCall
from learn_agent.policy import Workspace
from learn_agent.tools import ToolExecutor


def invoke(tools: ToolExecutor, name: str, **arguments: object):
    return tools.execute(ToolCall(id="x", name=name, arguments=json.dumps(arguments)))


def test_read_paging_and_hash(tmp_path: Path):
    (tmp_path / "note.txt").write_text("你好abc", encoding="utf-8")
    tools = ToolExecutor(Workspace(tmp_path))
    result = invoke(tools, "read_file", path="note.txt", offset=1, limit=2)
    assert result.ok and result.data["text"] == "好a"
    assert result.data["next_offset"] == 3 and len(result.data["sha256"]) == 64


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", ".env", ".env.prod", ".git/config"])
def test_path_denial(tmp_path: Path, path: str):
    result = invoke(ToolExecutor(Workspace(tmp_path)), "read_file", path=path, offset=0, limit=10)
    assert not result.ok


def test_symlink_and_directory_denial(tmp_path: Path):
    (tmp_path / "link").symlink_to(tmp_path.parent)
    tools = ToolExecutor(Workspace(tmp_path, allow_write=True))
    assert invoke(tools, "read_file", path="link/x", offset=0, limit=1).code == "symlink_denied"
    assert invoke(tools, "read_file", path=".", offset=0, limit=1).code == "not_regular_file"
    assert invoke(tools, "create_file", path="link/x", content="bad").code == "symlink_denied"


def test_write_grant_and_exclusive_create(tmp_path: Path):
    denied = invoke(ToolExecutor(Workspace(tmp_path)), "create_file", path="x", content="first")
    assert denied.code == "write_not_granted" and not (tmp_path / "x").exists()
    tools = ToolExecutor(Workspace(tmp_path, allow_write=True))
    assert invoke(tools, "create_file", path="x", content="first").ok
    assert invoke(tools, "create_file", path="x", content="second").code == "already_exists"
    assert (tmp_path / "x").read_text() == "first"


@pytest.mark.parametrize(
    "arguments",
    [
        "{",
        '{"path":"x","offset":"0","limit":1}',
        '{"path":"x","offset":true,"limit":1}',
        '{"path":"x","offset":0,"limit":99999}',
        '{"path":"x","offset":0,"limit":1,"extra":1}',
        '{"path":"x","path":"y","offset":0,"limit":1}',
        '{"path":"x","offset":NaN,"limit":1}',
        "[]",
    ],
)
def test_untrusted_json(tmp_path: Path, arguments: str):
    tools = ToolExecutor(Workspace(tmp_path))
    assert (
        tools.execute(ToolCall(id="x", name="read_file", arguments=arguments)).code
        == "invalid_arguments"
    )


def test_binary_large_and_missing_files(tmp_path: Path):
    tools = ToolExecutor(Workspace(tmp_path))
    (tmp_path / "binary").write_bytes(b"\xff")
    (tmp_path / "large").write_bytes(b"x" * 262_145)
    assert invoke(tools, "read_file", path="binary", offset=0, limit=1).code == "not_utf8"
    assert invoke(tools, "read_file", path="large", offset=0, limit=1).code == "file_too_large"
    assert invoke(tools, "read_file", path="missing", offset=0, limit=1).code == "not_found"
    assert invoke(tools, "exec_command", cmd="whoami").code == "unknown_tool"
