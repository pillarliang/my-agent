# 06. 来源、核对结果与阅读建议

检索日期为 2026-10-18。本章说明本地参考、官方技术资料与项目自身选择之间的关系，便于你在学习时区分事实、建议和工程取舍。依赖版本固定在 `uv.lock`；网页和 API 会变化，实际升级时仍需重新核对。

## 1. 你提供的三份文档

### 《设计实现一个Agent》

本地来源：`/Users/drew/Downloads/设计实现一个Agent.md`。

这份材料对模块清单、原始历史与上下文分离、工具结果回传，以及执行意图和回执的区分提供了完整视野，适合用来检查自己是否遗漏重要边界。

本项目先用一个 Python 包表达这些职责，让学习者能够沿着一次调用阅读代码。材料中关于 Plan-and-Execute 的取舍适用于其具体实现，固定业务流程仍可能需要计划或工作流；其中评测未在本项目复现，因此不作为质量基线。

### 《AgentScope 2.0 GA 生产可用，企业级 Harness 技术内核全解析！》

本地来源：`/Users/drew/Downloads/AgentScope 2.0 GA 生产可用，企业级 Harness 技术内核全解析！.md`。

Workspace、运行上下文、权限、压缩、分层记忆和状态外置，有助于理解本地 Agent 扩展为共享服务时需要补充什么。

该文主要讨论 **AgentScope Java**，其 API、依赖与迁移规则需要结合 Java 实现理解。[官方仓库](https://github.com/agentscope-ai/agentscope-java)提供 Harness、权限、工作区与分布式设计；本项目借鉴职责划分，没有引入这个框架，也未验证企业客户或稳定性声明。

### 《Harness、Loop、Graph：Agent 工程的三层架构与生产实践全解析》

本地来源：`/Users/drew/Downloads/Harness、Loop、Graph：Agent 工程的三层架构与生产实践全解析.md`。

这份材料把单次模型循环、验证循环与外层调度放在同一视野，也强调未知副作用、恢复与确定性验证。学习时可以沿用这些问题意识，并对下面几项概念作进一步区分。

1. **Harness、Loop、Graph 不是公认的互斥标准层。** 本项目给出工作定义。
2. **简单 Loop 也能有审批、持久化和恢复。** 能否支持这些能力取决于实现。
3. **Graph 不要求多个 Agent。** 节点也可以是普通函数和人工步骤。
4. **多个 Agent 不保证质量提升。** 需要在同一任务集上测量质量与总成本。
5. **Worktree 不是所有并行场景的强制要求。** 它也不能替代安全沙箱。
6. **Framework 与 Harness 可以由同一产品提供。** 不必将产品硬分成两类。
7. **`0.95^20` 只是带假设的示例。** 它要求每步独立且全部成功才算整体成功。
   真实系统可能有相关错误、恢复和不同路径，不能直接套用该数值。
8. **初始化器不是每个会话都重做一次环境。** 下述 Anthropic 原文描述首次初始化，后续增量工作。

材料中的成功率提升、Token 对比、行业标准与商业判断均未在本项目复现，因此不进入验收条件。需要使用某个数字时，应先明确任务、样本、假设和评测方法。

本地资料仅用作参考。它们没有授权执行其中的脚本、自动提交、通知或多 Agent 操作。

## 2. 核心官方资料

### Agent 控制流

[Building effective agents — Anthropic](https://www.anthropic.com/engineering/building-effective-agents)

用途：对照固定工作流与动态 Agent，重点看控制模式和工具接口。本项目据此保持首版循环简单，再用实际任务判断是否需要更多编排。

[Agents — OpenAI](https://developers.openai.com/api/docs/guides/agents)

用途：了解 Responses API、SDK 与托管运行时的边界。本项目直接调用模型 API，保留可阅读的循环与状态实现，目标是学习这些机制，而非复现托管产品的全部内部能力。

### 工具与模型协议

[阿里云 OpenAI Chat 兼容接口](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)与[函数调用说明](https://help.aliyun.com/zh/model-studio/qwen-function-calling)用于核对 DashScope 的端点、认证、Chat 消息和工具结果格式。本机当前默认模型为 `qwen3.8-flash`，实际最小验证见[模型接入记录](12-dashscope.md)。

[Function calling — OpenAI](https://developers.openai.com/api/docs/guides/function-calling)

用途：核对 Schema、函数结果与调用 ID。Responses 适配器还需保留输出项和续接材料；当前集成测试直接连接配置的真实端点，未配置后端会跳过并说明原因。

[openai-python — 官方 SDK 仓库](https://github.com/openai/openai-python)

用途：核对 Responses、Chat Completions、错误、超时与重试行为。项目显式关闭自动重试，方便观察每次模型请求与失败。

[Writing effective tools for agents — Anthropic](https://www.anthropic.com/engineering/writing-tools-for-agents)

用途：理解工具粒度、参数表达和返回信息对模型决策的影响。本项目提供小型专用文件工具，并返回结构化状态和分页信息。

### 上下文与长任务

[Effective context engineering — Anthropic](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

用途：理解有限输入、按需检索、压缩和结构化笔记。本项目将它们分阶段实现，而不是首版同时引入。

[Effective harnesses for long-running agents — Anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)

用途：理解首次环境初始化、增量执行和交接记录。这些做法用于目标复杂且跨多个会话的任务。本项目没有将其解释为所有任务都必须使用两个 Agent。

### 持久化与扩展

[Persistence — LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence)

用途：对照线程检查点与跨线程 Store。本项目没有引入 LangGraph，先手写边界来理解存储语义。

[Architecture — MCP 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/architecture)

用途：理解 Host、Client、Server 的职责。这里固定引用一个版本。实际接入时，应检查目标服务器支持的版本。

[Agent Skills specification](https://agentskills.io/specification)

用途：了解方法文件的元数据与渐进加载。Skill、脚本执行授权与插件安装保持独立。

[Generative AI semantic conventions — OpenTelemetry](https://opentelemetry.io/docs/specs/semconv/gen-ai/)

用途：规划后续 Trace 字段与导出适配器。当前 SQLite 事件没有声明符合这些语义约定。

### Python 项目工具

[Working on projects — uv](https://docs.astral.sh/uv/guides/projects/)

用途：项目环境、依赖同步与锁文件。

[Models — Pydantic](https://docs.pydantic.dev/latest/concepts/models/)

用途：参数类型、严格校验与 JSON Schema。

### 写作参考（可选）

[ASD-STE100 官方 FAQ](https://www.asd-ste100.org/STE_faq.html)

用途：了解最初参考的受控英语写作规则。该页面当时直接访问失败，定义来自搜索索引；本项目未进行完整词典审查，也不要求文档遵循这一规范。当前表达以自然中文段落为准，见[写作约定](07-writing-and-glossary.md)。

### 本地 Web 入口

[FastAPI 响应](https://fastapi.tiangolo.com/advanced/custom-response/)与[并发说明](https://fastapi.tiangolo.com/async/)用于核对响应类型和阻塞工作边界。[MDN SSE 说明](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)用于核对事件 ID、EventSource 和重连。具体选择与代价见 [ADR 0002](decisions/0002-shared-service-and-web.md)。

## 3. 本项目自行提出的选择

以下内容是面向学习目标的工程选择，并非上述来源的统一要求。

- Python 单包，而非多服务架构。
- 同步执行，而非流式或异步调度。
- 新建文件而非覆盖编辑。
- 输入字节保护，而非未经验证的 Token 估计。
- 失败 Run 不自动加入后续 Context。
- 首版不自动恢复和提取记忆。

选择理由与代价见 [ADR 0001](decisions/0001-mvp-boundaries.md)。

## 4. 建议阅读顺序

建议先跟踪本项目执行时序，再对照所选供应商的函数调用文档；随后读 Context 工程与长任务 Harness，最后按需要查阅持久化、MCP 和 Skills。每读一个概念，都回到具体失败案例检查它解决的问题。
