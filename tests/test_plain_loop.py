"""阶段 0 的本地检查：真实文件 I/O + 本机 HTTP 协议，不调用外部模型。"""

import ast
import importlib.util
import json
from pathlib import Path

import pytest
from protocol_support import completion, local_api

SCRIPT = Path(__file__).resolve().parents[1] / "examples/00_plain_loop.py"
spec = importlib.util.spec_from_file_location("plain_loop", SCRIPT)
plain_loop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plain_loop)


def test_script_has_no_project_package_dependency():
    dependencies = []
    for node in ast.walk(ast.parse(SCRIPT.read_text())):
        if isinstance(node, ast.Import):
            dependencies.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            dependencies.append(node.module or "")
    assert not any(name.startswith("learn_agent") for name in dependencies)


def test_tool_reads_actual_utf8_file_and_reports_failure(tmp_path):
    (tmp_path / "notes.txt").write_text("学习目标：理解循环。LEARN-42", encoding="utf-8")
    result = plain_loop.execute_tool("read_file", '{"path":"notes.txt"}', tmp_path)
    assert result == {"ok": True, "path": "notes.txt", "text": "学习目标：理解循环。LEARN-42"}
    assert not plain_loop.execute_tool("read_file", '{"path":"missing.txt"}', tmp_path)["ok"]


@pytest.mark.parametrize(
    "arguments",
    [
        "not-json",
        "[]",
        '{"path":1}',
        '{"path":"x","extra":true}',
        '{"path":"../outside.txt"}',
        '{"path":"/etc/passwd"}',
        '{"path":".env"}',
    ],
)
def test_tool_rejects_invalid_or_outside_input(tmp_path, arguments):
    assert not plain_loop.execute_tool("read_file", arguments, tmp_path)["ok"]


def test_tool_rejects_unknown_large_or_non_utf8_input(tmp_path):
    assert plain_loop.execute_tool("shell", "{}", tmp_path)["error"] == "unknown_tool"
    (tmp_path / "large.txt").write_bytes(b"a" * 16_001)
    assert (
        "读取上限"
        in plain_loop.execute_tool("read_file", '{"path":"large.txt"}', tmp_path)["error"]
    )
    (tmp_path / "binary.txt").write_bytes(b"\xff")
    assert (
        plain_loop.execute_tool("read_file", '{"path":"binary.txt"}', tmp_path)["error"]
        == "文件不是有效的 UTF-8 文本。"
    )


@pytest.mark.parametrize("tool_count", [1, 2])
def test_real_sdk_sends_tool_result_in_next_request(tmp_path, tool_count):
    (tmp_path / "notes.txt").write_text("LEARN-42", encoding="utf-8")
    with local_api(
        [
            completion(path="notes.txt", finish="tool_calls", count=tool_count),
            completion(text="LEARN-42"),
        ]
    ) as (client, requests):
        answer = plain_loop.run_agent(client, "local", "读取文件", tmp_path)
    assert answer == "LEARN-42" and len(requests) == 2
    first, second = requests
    assert first["tools"][0]["function"]["name"] == "read_file"
    assert [message["role"] for message in second["messages"]] == [
        "system",
        "user",
        "assistant",
        *(["tool"] * tool_count),
    ]
    calls = second["messages"][2]["tool_calls"]
    results = second["messages"][3:]
    assert len(calls) == len(results) == tool_count
    for call, result in zip(calls, results, strict=True):
        assert call["id"] == result["tool_call_id"]
        assert json.loads(result["content"]) == {
            "ok": True,
            "path": "notes.txt",
            "text": "LEARN-42",
        }


def test_failed_tool_read_also_enters_next_request(tmp_path):
    with local_api(
        [completion(path="missing.txt", finish="tool_calls"), completion(text="读取失败")]
    ) as (client, requests):
        assert plain_loop.run_agent(client, "local", "读取文件", tmp_path) == "读取失败"
    assert json.loads(requests[1]["messages"][-1]["content"])["ok"] is False


def test_main_runs_from_configuration_and_prints_answer(tmp_path, monkeypatch, capsys):
    (tmp_path / "notes.txt").write_text("LEARN-42", encoding="utf-8")
    monkeypatch.setattr(plain_loop, "WORKSPACE", tmp_path)
    with local_api(
        [completion(path="notes.txt", finish="tool_calls"), completion(text="LEARN-42")]
    ) as (client, requests):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "local-protocol-test")
        monkeypatch.setenv("DASHSCOPE_BASE_URL", str(client.base_url))
        monkeypatch.setenv("DASHSCOPE_MODEL", "local")
        monkeypatch.setenv("DASHSCOPE_USE_ENV_PROXY", "false")
        plain_loop.main()
    assert len(requests) == 2
    assert "模型说明：\nLEARN-42" in capsys.readouterr().out


def test_step_limit_bounds_requests(tmp_path):
    (tmp_path / "notes.txt").write_text("资料", encoding="utf-8")
    with local_api([completion(path="notes.txt", finish="tool_calls")]) as (client, requests):
        with pytest.raises(RuntimeError, match="2 次模型请求上限"):
            plain_loop.run_agent(client, "local", "读取文件", tmp_path, max_steps=2)
    assert len(requests) == 2


@pytest.mark.parametrize(
    "response",
    [
        completion(),
        completion(text="截断", finish="length"),
    ],
)
def test_invalid_reply_stops_before_tool_feedback(tmp_path, response):
    with local_api([response]) as (client, requests):
        with pytest.raises(RuntimeError):
            plain_loop.run_agent(client, "local", "读取文件", tmp_path)
    assert len(requests) == 1


def test_unsupported_tool_type_stops_at_response_boundary(tmp_path):
    response = completion(finish="tool_calls")
    response["choices"][0]["message"]["tool_calls"] = [
        {"id": "custom_0", "type": "custom", "custom": {"name": "shell", "input": "pwd"}}
    ]
    with local_api([response]) as (client, requests):
        with pytest.raises(RuntimeError, match="只支持函数工具"):
            plain_loop.run_agent(client, "local", "读取文件", tmp_path)
    assert len(requests) == 1
