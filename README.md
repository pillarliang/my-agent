# Learn Agent：从一个独立 Loop 开始

从 **阶段 0：单文件 Agent Loop** 开始。打开 [examples/00_plain_loop.py](examples/00_plain_loop.py)，先看 `run_agent()`。

**教程入口：[docs/tutorial.html](docs/tutorial.html#stage-0)。** 当前实现与未来设计明确分开；后续从这个文件逐步提取模块、增加能力。

## 运行

需要 Python 3.12 或更新版本与 uv。在项目根目录执行：

```sh
uv sync --locked
```

参照 [.env.example](.env.example) 创建 `.env`，配置有效的 DashScope Key、匹配的 HTTPS 端点和可用模型。已有 `.env` 可以继续使用，已有环境变量优先。

```sh
uv run python examples/00_plain_loop.py
```

程序会请求真实模型并产生 API 用量。它读取 [notes.txt](examples/workspace/notes.txt)，打印每轮请求与工具回执，再输出模型说明。默认最多四次请求，工具仅支持读取工作区中的小型 UTF-8 文件。

## 从哪里读

打开 [examples/00_plain_loop.py](examples/00_plain_loop.py)，按职责阅读：

1. `TOOLS`：模型能选择什么动作、需要什么参数。
2. `read_file()`：实际文件读取与资源范围。
3. `execute_tool()`：参数如何传入工具，以及成功或失败如何形成回执。
4. `run_agent()`：消息如何追加，模型和工具如何形成反馈循环。
5. `main()`：配置如何加载，SDK 客户端如何创建与关闭。

当前直接使用模型 API 的字典消息。`assistant.tool_calls[].id` 与 `tool.tool_call_id` 关联动作和结果。

下一阶段先从这里提取消息契约、模型调用与工具执行边界，再逐步实现存储、上下文预算、权限、验收和应用入口。完整模块方案仍在教程中，作为后续设计参考。

模块按实际变化与协作需要提取，函数和字典已够用时保持简单。保留必要的输入与资源检查，避免重复判断、宽泛捕获和提前建设框架。持续约定见[项目设计规则](.cursor/rules/learning-design.mdc)。

## 本地检查

以下命令不请求外部模型：

```sh
uv run ruff check examples scripts tests
uv run ruff format --check examples scripts tests
uv run mypy
uv run pytest
uv run python scripts/check_docs.py
```

测试使用临时文件和本机 HTTP 服务检查协议与循环，不代表真实模型的任务能力已通过验证。

教程直接维护 `docs/tutorial.html` 中的正文与 SVG。



参考：[file:///Users/drew/Documents/Codex/2026-10-19/qing/outputs/agent-system-architecture.html](file:///Users/drew/Documents/Codex/2026-10-19/qing/outputs/agent-system-architecture.html)
