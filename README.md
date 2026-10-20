# Learn Agent：从独立 Loop 到工具与模型边界

教程入口：[系统职责地图](docs/tutorial.html#system-map)。按[学习主线](docs/tutorial.html#learning)逐步实现，按职责查通用原理；当前已完成[阶段 1：Tools 与 Model](docs/tutorial.html#stage-1)。

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

两种入口都请求真实模型并产生 API 用量，默认最多四次请求。阶段 1 在 [notes.txt](examples/workspace/notes.txt) 中搜索验收标记，输出模型说明；工具选择与回答质量需实际运行观察。

## 从哪里读

阶段 0 从 [run_agent()](examples/00_plain_loop.py) 开始。阶段 1 按教程中的顺序阅读：

1. [工具契约](learn_agent/tools/contracts.py)：定义、参数、调用、上下文、回执和执行器接口。
2. [应用入口](learn_agent/__main__.py)：显式选择工具并注入工作区。
3. [注册与分发](learn_agent/tools/registry.py)：生成声明、校验参数、执行与异常分流。
4. [只读文件工具](learn_agent/tools/filesystem.py)：`read_file` 与单文件 `search_text`。
5. [模型服务](learn_agent/models/service.py)、[模型契约](learn_agent/models/contracts.py)与[协议适配器](learn_agent/models/openai_chat.py)：配置与鉴权、连接生命周期、请求与回复均由模型模块管理。
6. [Loop 接入](learn_agent/runner.py)：请求模型、保存回复、执行工具并追加结果。

添加工具时遵守同一套规则；模型协议只在适配器中处理，Runner 不依赖 SDK 消息格式。后续按需求增加执行环境、写入授权、验收和状态机制。完整范围、实验与验收条件直接维护在教程中。

保持函数和模块职责清楚，避免提前建设框架。持续约定见[项目设计规则](.cursor/rules/learning-design.mdc)。

## 本地检查

以下命令不请求外部模型：

```sh
uv run ruff check learn_agent examples scripts tests
uv run ruff format --check learn_agent examples scripts tests
uv run mypy
uv run pytest
uv run python scripts/check_docs.py
```

测试读取真实临时文件，并让实际 SDK 请求本机 HTTP 服务，检查注册、参数与资源边界、异常、消息关联和第三个工具接入，并验证模型用量、原生材料续接、停止原因、HTTP 错误与超时。固定协议响应不证明真实模型的任务能力。

教程直接维护 `docs/tutorial.html` 中的正文与 SVG，不添加文档生成流水线。

参考：[早期架构资料](file:///Users/drew/Documents/Codex/2026-10-19/qing/outputs/agent-system-architecture.html)
