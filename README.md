# Learn Agent：从独立 Loop 到受控工具执行

教程入口：[系统职责地图](docs/tutorial.html#system-map)。[阶段 1：Tools 与 Model](docs/tutorial.html#stage-1) 已完成；[阶段 2](docs/tutorial.html#stage-2) 已实现 Policy 与受控创建，独立产物验收待实现。

## 安装与运行

需要 Python 3.12 或更新版本与 uv；在项目根目录执行：

```sh
uv sync --locked
```

参照 [.env.example](.env.example) 创建 `.env`，配置有效的 DashScope Key、匹配的 HTTPS 端点和可用模型。已有环境变量优先。

阶段 0：独立的最小反馈循环，不依赖 `learn_agent` 包。

```sh
uv run python examples/00_plain_loop.py
```

阶段 1：工具统一注册与执行；LLMService 内部管理配置和 SDK 连接，Runner 通过 invoke() 调用。

```sh
uv run python -m learn_agent
```

阶段 2：显式授权新建指定文件；已有目标绝不覆盖。

```sh
uv run python -m learn_agent --create-file policy-report.md
```

默认任务搜索 [notes.txt](examples/workspace/notes.txt) 的验收标记，再请求创建报告。`--workspace` 指定工作区，`--question` 自定义任务文本；文本不能扩大 `--create-file` 授权。未授权路径返回 `policy_denied`，已有目标返回 `file_exists`。创建仅支持 macOS / Linux，父目录须存在，不跟随符号链接，内容最多 16,000 个 UTF-8 字节。

以上入口均请求真实模型并产生 API 用量，默认最多四次请求。工具选择与回答质量需实际运行观察；本地测试可确定性检查权限与实际文件效果。

## 从哪里读

阶段 0 从 [run_agent()](examples/00_plain_loop.py) 开始。阶段 1 按教程中的顺序阅读：

1. [工具契约](learn_agent/tools/contracts.py)：定义、参数、调用、上下文、回执和执行器接口。
2. [应用入口](learn_agent/__main__.py)：显式选择工具并注入工作区。
3. [注册与分发](learn_agent/tools/registry.py)：生成声明、校验参数、执行与异常分流。
4. [只读文件工具](learn_agent/tools/filesystem.py)：`read_file` 与单文件 `search_text`。
5. [模型服务](learn_agent/models/service.py)、[模型契约](learn_agent/models/contracts.py)与[协议适配器](learn_agent/models/openai_chat.py)：配置与鉴权、连接生命周期、请求与回复均由模型模块管理。
6. [Loop 接入](learn_agent/runner.py)：请求模型、保存回复、执行工具并追加结果。

阶段 2 先读 [Policy 与决策接口](learn_agent/policy.py)，再沿文件工具的 `create_file()` 阅读授权与独占创建；[验收用例](tests/test_policy.py) 检查实际文件与回执。

添加有副作用的工具时，在资源执行边界调用 `PolicyEvaluator.evaluate(ActionRequest)`。策略由应用通过 `ToolContext` 注入，Runner 仍只提交 `ToolCall`、接收 `ToolResult`。当前在本地进程执行，后续按需增加独立验收、状态与隔离。完整范围与黑盒交互图直接维护在教程中。

保持函数和模块职责清楚，避免提前建设框架。代码遵循[项目设计规则](.cursor/rules/learning-design.mdc)，补充教程遵循[教程写作规范](docs/tutorial-writing.md)；代码助手入口见 [AGENTS.md](AGENTS.md)。

## 本地检查

以下命令不请求外部模型：

```sh
uv run ruff check learn_agent examples scripts tests
uv run ruff format --check learn_agent examples scripts tests
uv run mypy
uv run pytest
uv run python scripts/check_docs.py
```

测试操作真实临时文件，并让实际 SDK 请求本机 HTTP 服务；覆盖授权与拒绝、禁止覆盖、并发与符号链接，以及工具注册、资源边界、消息关联和模型协议。固定协议响应不证明真实模型的任务能力。

教程直接维护 `docs/tutorial.html` 中的正文与 SVG，不添加文档生成流水线。

参考：[早期架构资料](file:///Users/drew/Documents/Codex/2026-10-19/qing/outputs/agent-system-architecture.html)
