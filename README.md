# Learn Agent：从独立 Loop 到受控工具执行

教程入口：[模块职责图](docs/tutorial.html#system-map)。阶段 0–2 已实现；[阶段 3A：会话与记录](docs/stages/stage-3a.html#stage-3a) 与[阶段 3B：上下文管理与预算](docs/stages/stage-3b.html#stage-3b) 均为后续设计练习。

模块教程结合 pi、Codex 等 Agent 的源码比较实现原理；阶段实践只选择当前需要的一种方案，逐步完成各核心模块的 MVP。先让选定的核心模块串成端到端 Agent，整体跑通后再按真实问题做局部优化。参考方案用于理解与取舍，不要求全部实现。

## 启动教程网页

在项目根目录运行独立静态服务，仅需 Python 3.12 或更新版本：

```sh
python3 scripts/serve_tutorial.py --port 8080
```

打开 <http://localhost:8080/>；远程访问时将 localhost 替换为服务器地址。默认监听 `0.0.0.0`，可用 `--host 127.0.0.1` 限制为本机访问；按 Ctrl+C 停止。

服务提供教程、共享资源和源码阅读链接，修改文档后刷新即可。脚本独立于 Agent，不需要安装项目依赖或配置 API Key。

## 安装与运行

需要 Python 3.12 或更新版本与 uv；在项目根目录执行：

```sh
uv sync --locked
```

参照 [.env.example](.env.example) 创建 `.env`，配置有效的 DashScope Key、匹配的 HTTPS 端点和可用模型。已有环境变量优先。

通用入口：启动后输入自然语言任务，不需要指定文件路径或输出文件名。

```sh
uv run python -m learn_agent
```

也可以直接提供任务文本：

```sh
uv run python -m learn_agent "看看有哪些资料，整理成一份摘要并保存"
```

当前每次启动处理一个独立任务。消息与工具回执只保留在本次运行的内存中，不写入数据库，也不自动带入下一次启动。Runner 发送系统指令、本次消息与工具定义，默认输出上限为 512 Token、最多请求模型四次。

[阶段 3A：会话与记录](docs/stages/stage-3a.html#stage-3a) 是待实现的设计练习：参考 pi 与 Codex 的数据组织方式，先约定保存、查询与历史重建，再接入循环。当前没有会话续接或历史查询命令；输入预算、裁剪与自动恢复也尚未实现。

工作目录和权限在 [入口](learn_agent/__main__.py) 设置，工具清单写在 [application.py](learn_agent/application.py) 的 DEFAULT_TOOLS 中，不随问题变化。默认工作目录是 `examples/workspace`，启动时会显示；启用 `list_files`、`read_file`、`search_text`、`write_file`，由模型选择调用。通用入口允许在工作目录内读取、新建文件，禁止隐藏路径、越界和覆盖已有文件。写入仅支持 macOS / Linux，父目录须存在，正文最多 16,000 个 UTF-8 字节。

阶段 2 验证 Policy 的“禁止覆盖”，不请求模型：

```sh
uv run python -m examples.policy_no_overwrite
```

实验通过真实工具先新建、再尝试覆盖同一文件。预期第二次返回 `policy_denied`，原内容未变。

阶段 0 的独立闭环仍可运行：

```sh
uv run python examples/00_plain_loop.py
```

通用 Agent 和阶段 0 入口会请求真实模型并产生 API 用量，默认最多四次请求；Policy 实验不请求模型。本地固定协议响应只检查程序行为，工具选择与真实模型表现需单独观察。

## 从哪里读

阶段 0 从 [run_agent()](examples/00_plain_loop.py) 开始。阶段 1 按教程中的顺序阅读：

1. [工具契约](learn_agent/tools/contracts.py)：定义、参数、调用、回执和执行器接口。
2. [用户入口](learn_agent/__main__.py)：接收任务，设置工作区和权限，再运行、展示；[application.py](learn_agent/application.py) 的 run_task() 注册工具、创建模型、调用 Runner 并关闭连接，只返回运行结果。
3. [注册与分发](learn_agent/tools/registry.py)：生成声明、校验参数、识别动作、统一授权、执行与异常分流。
4. [文件工具](learn_agent/tools/filesystem.py)：`list_files` 发现文件，再通过 `read_file`、单文件 `search_text` 读取或搜索，通过 `write_file` 请求新建或覆盖。
5. [模型服务](learn_agent/models/service.py)、[模型契约](learn_agent/models/contracts.py)与[协议适配器](learn_agent/models/openai_chat.py)：配置与鉴权、连接生命周期、请求与回复均由模型模块管理。
6. [Loop 接入](learn_agent/runner.py)：请求模型、保存回复、执行工具并追加结果；返回含模型说明与本次回执的 `RunResult`。

阶段 2 先读 [Tools ↔ Policy 协议](docs/modules/policy.html#module-policy-interface)：工具识别动作，注册表以 `ActionRequest(operation, resource)` 统一询问 `Policy.evaluate()`，取得 `PolicyDecision(allowed, reason)`。拒绝返回 `policy_denied`；允许后才执行。[禁止覆盖实验](examples/policy_no_overwrite.py) 和 [Policy 测试](tests/test_policy.py) 验证权限是否生效，不涉及成果验收。

当前代码实现到阶段 2。后续先读 [Session / State 模块原理](docs/modules/state.html#session-purpose)，理解交互尺度、消息构成、内存状态与持久记录，再按 [阶段 3A 的设计练习](docs/stages/stage-3a.html#stage-3a) 逐步实现。阶段 3A 与 [3B](docs/stages/stage-3b.html#stage-3b) 目前均未接入运行代码。

普通函数注册时直接声明所需权限，例如 `tools.register(count_lines, ReadFileArguments, permission="read", path_parameter="path")`。路径来自本次调用的 path 参数；不再传动作说明函数。注册表统一生成动作、调用 Policy，允许才执行。声明需求不代表获得授权；write 会根据目标是否存在区分新建与覆盖。新增工具复用已有权限即可，不改 registry.py 或 Runner。

[count_lines 示例](examples/count_lines.py) 同时演示读取获准与拒绝，不调用模型：

```sh
uv run python -m examples.count_lines
```

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

测试操作真实临时文件，并让实际 SDK 请求本机 HTTP 服务；覆盖不提供文件名的自然语言任务、按配置注册工具、目录发现、授权与拒绝、禁止覆盖、并发与符号链接，以及工具注册、资源边界、消息关联和模型协议。另检查独立任务的模型输入不会自动混入前一次调用的消息。会话与持久记录的验收要求在阶段 3A 中作为后续练习说明。固定协议响应不证明真实模型的任务能力；真实模型在连续任务中的表现需单独运行观察。

教程以 `docs/tutorial.html` 为总目录，阶段与模块分别在 `docs/stages/`、`docs/modules/` 中直接维护 HTML；共享样式、交互与复用 SVG 在 `docs/assets/`。浏览器可直接打开，不需要文档构建。编辑时按章节读取，避免把全书及图源一起带入上下文。

参考：[当前骨架与架构蓝图](docs/reference/architecture-agent-harness.html#architecture-agent-harness)
