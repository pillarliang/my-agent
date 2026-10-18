# 08. 第一次修改：增加一个工具

本章通过增加 `count_characters` 练习工具扩展路径，这个工具尚未加入 MVP。你可以按步骤修改、验证并对照现有注册表，观察为什么一个新能力不需要改变 Agent Loop。

## 1. 先看最小循环

```sh
uv run python examples/00_plain_loop.py
```

这个示例直接使用 DashScope `qwen3.8-flash`，让模型提出读取文件的调用，再将真实结果交回模型。它复用受限文件工具和模型适配器，省略持久化与 Web 层，方便只关注输入、动作和反馈；运行时需要凭证并产生 API 调用。

然后打开 `runner.py`，找到相同的四个动作。

1. 请求模型。
2. 保存响应。
3. 执行工具。
4. 追加工具结果。

## 2. 定义工具输入

在 `src/learn_agent/tools.py` 的参数类后增加：

```python
class CountArgs(Arguments):
    path: str
```

`Arguments` 已经配置严格类型与禁止多余字段，新工具继承它即可共用现有规则，让对外 Schema 与实际参数校验保持一致。

## 3. 编写工具方法

在 `ToolExecutor` 中增加：

```python
def _count(self, args: CountArgs) -> dict[str, Any]:
    page = self._read(ReadArgs(path=args.path, offset=0, limit=1))
    return {
        "path": args.path,
        "total_chars": page["total_chars"],
        "sha256": page["sha256"],
    }
```

`_read` 会检查路径、普通文件、256 KiB 大小上限与 UTF-8，并计算全文字符数和哈希。本工具复用这些边界，只请求一个字符的预览，因此设置 `limit=1`，再从结果中取得计数与版本。

统计单位是 Python 字符，也就是 Unicode 码位。字节数、显示宽度与用户感知的字形可能不同，例如部分 emoji 和组合字符包含多个码位，说明单位可以避免使用者误解结果。

## 4. 注册工具

在 `ToolExecutor.__init__()` 的 `_tools` 字典中增加：

```python
"count_characters": (
    CountArgs,
    "Count Unicode code points in a UTF-8 workspace file up to 256 KiB.",
    self._count,
),
```

注册后，`specs` 会自动提供新工具定义，`execute()` 继续负责参数检查与错误转换，Runner 仍通过统一协议调用工具。

## 5. 编写一个行为测试

新建 `tests/test_count_characters.py`：

```python
from learn_agent.contracts import ToolCall
from learn_agent.policy import Workspace
from learn_agent.tools import ToolExecutor

def test_counts_unicode_characters(tmp_path):
    (tmp_path / "note.txt").write_text("你好abc", encoding="utf-8")
    tools = ToolExecutor(Workspace(tmp_path))
    result = tools.execute(ToolCall(
        id="count_1",
        name="count_characters",
        arguments='{"path":"note.txt"}',
    ))
    assert result.ok
    assert result.data["total_chars"] == 5
```

运行：

```sh
uv run pytest tests/test_count_characters.py
uv run ruff check src tests
uv run mypy
```

再增加越界路径和无效 UTF-8 案例，确认新工具复用了原有读取范围与错误语义，而没有因为内部调用而放宽限制。

## 6. 接入真实模型

完成代码修改与测试后，可以使用本机已配置的 DashScope `qwen3.8-flash` 观察模型是否选用新工具：

```sh
uv run learn-agent run "计算 notes.txt 的字符数，并说明统计单位" \
  --provider dashscope --trace
```

真实模型可能选择新工具，也可能用原有 `read_file` 回答。需要根据事件确认选择路径，再分析工具说明是否足够明确；最终答案正确并不能单独证明调用了你新增的工具。

## 7. 解释修改的传播路径

```text
CountArgs
  -> specs 生成参数 Schema
  -> Model 看到定义并提出调用
  -> execute 校验参数
  -> _count 复用受限读取
  -> ToolResult 进入 Transcript
  -> 下一次 Context 包含结果
```

**验收条件：** 能指出每个箭头对应的代码位置，解释为什么 Loop 无需修改，并用事件证明新工具经过了统一的校验与结果回传。
