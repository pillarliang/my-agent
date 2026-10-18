# 10. Web UI：运行与学习

## 1. Web UI 与 CLI 的关系

项目提供 Web UI 和 CLI 两个并列入口，它们都通过 `AgentService` 调用同一套 Runner、工具、Context 和 SQLite。你可以直接启动 Web 服务，不需要先执行一次 CLI；CLI 也不依赖 Web 服务运行。入口只决定如何提交请求和展示结果，模型、权限与执行逻辑由共同的应用层管理。

Web UI 可以新建会话、发送消息、查看回答，展示模型请求与工具执行进度，并支持展开工具参数和结构化结果。会话与运行记录保存在数据库中，因此可以切换历史、刷新页面，也能查看使用相同数据库的 CLI 记录。

## 2. 启动 Web UI

本机已配置 DashScope，默认模型为 `qwen3.8-flash`，建议先用真实交互体验系统。前端使用项目自带的 HTML、CSS 和 JavaScript，没有 Node.js 构建步骤，也不依赖外部 CDN。

```sh
cd /Users/drew/Documents/dev/learn-agent
uv sync --locked --extra web
uv run --extra web learn-agent serve --provider dashscope --allow-write
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，确认模型标签为 `DashScope · qwen3.8-flash`，然后尝试下面的交互：

1. 点击“新建会话”，或直接输入“读取 notes.txt，说明其中的学习目标”。
2. 发送后观察模型请求与 `read_file` 的开始、结束事件，展开工具结果，确认回答使用了文件内容。
3. 继续追问，再刷新页面或切换会话，检查历史是否保留。

服务运行时保留终端，按 `Ctrl+C` 停止。`--allow-write` 允许在工作区内新建文件，去掉后保持只读；浏览器不能通过消息或请求参数取得额外写权限。默认工作区为 `examples/workspace`，端点、代理与环境变量说明见 [DashScope 教程](12-dashscope.md)。

## 3. 切换真实模型后端

默认使用已配置的 DashScope `qwen3.8-flash`，也可以切换到 OpenAI。模型与权限由后端启动配置决定，页面只提交任务和展示结果。

### OpenAI

按照 [README](../README.md#3-接入真实模型)在本地 `.env` 中配置 OpenAI Key 与模型名，再执行以下命令。密钥由 Python 后端读取，不在浏览器中输入。

```sh
uv run --extra web learn-agent serve --provider openai --allow-write
```

浏览器始终访问本地 Python API，后端再通过所选适配器请求模型。修改模型、端点或工作区后，需要重启服务并创建新会话；旧会话可以查看，但绑定不匹配时不能继续。当前 DashScope 已通过最小真实工具调用验证，OpenAI 原生端点尚未实测，验证范围见[验证记录](09-validation.md)。

## 4. CLI 与 Web 共享数据

两种入口默认使用当前目录下的 `.learn-agent/state.sqlite3`。请在项目根目录启动，或通过全局 `--db` 指定同一路径。会话标题与历史来自数据库，不依赖创建它的入口。

例如，在 Web 服务运行时，另开终端执行下面的只读请求，再点击页面左侧“刷新”，就能看到 `flash-cli-lesson` 会话。这条命令会调用真实模型；模型、端点与工作区匹配时，可以继续在 Web 中交互。

```sh
cd /Users/drew/Documents/dev/learn-agent
uv run learn-agent run "读取 notes.txt，说明学习目标" \
  --provider dashscope --session flash-cli-lesson
```

如果需要独立的学习数据库，可以分别在两个终端运行以下命令。`--db` 放在 `serve` 或 `run` 前面：

```sh
uv run --extra web learn-agent --db .learn-agent/shared.sqlite3 \
  serve --provider dashscope
```

```sh
uv run learn-agent --db .learn-agent/shared.sqlite3 \
  run "读取 notes.txt，说明学习目标" --provider dashscope --session shared-flash-lesson
```

页面每次展示最近 20 个 Run，点击“加载更早的记录”继续读取。会话列表最多显示 100 个会话，按创建顺序倒序排列。这是执行历史的查询与展示，跨会话知识选择和长期记忆仍需后续实现。

## 5. 如何读界面

左侧展示持久会话，标题取自首个用户请求的前 80 个字符；中间展示用户消息、模型文字和可展开的工具观察；右侧展示当前选中 Run 的事件。点击旧 Run 的“查看过程”，可以重新读取对应事件。

每个 Run 优先显示系统结论卡片，再展示工具观察和“模型说明”。没有检查器时卡片明确显示“结果未验证”，检查失败时显示失败，只有指定检查通过才使用绿色的“已通过指定检查”，并列出目标、条件与哈希。侧栏中的“循环已结束”只是运行状态，不表示任务已经验收。SSE 当前推送执行事件，最终回答在 Run 结束后读取显示，尚无模型文字逐 Token 输出。

## 6. 刷新、断线与退出

刷新页面不会重新运行任务：页面先读取 SQLite 历史，再补读当前 Run 的事件。同一 SSE 连接断开后，浏览器带最后的事件 ID 重连；关闭页面只会断开观察连接，已经接受的任务继续执行。

服务最多同时执行 4 个 Web Run，同一会话最多执行一个。不同会话仍可能操作相同工作区，当前没有跨会话文件锁，因此应避免同时创建同一路径。

在服务终端按 `Ctrl+C`，服务器会等待已经接受的任务结束。当前没有浏览器取消按钮，也不能强制终止工作线程；强制退出可能留下 `running` 状态。遇到这种情况，按[崩溃处理步骤](04-boundaries.md#7-崩溃后处理)核对事件与实际文件，再显式放弃旧 Run。

## 7. 本地服务边界

服务只绑定 `127.0.0.1`，通过 Host、同源检查与自定义请求头限制浏览器调用，尚无账户登录或多租户隔离。它面向本地学习，直接转发到公共网络会超出当前设计边界。

浏览器仅加载项目脚本与样式，用户输入、模型文字和工具结果均作为纯文本渲染。HTTP 历史不返回 `provider_items` 中的不透明协议材料，API Key 也不进入前端配置。

## 8. 常见问题

- **缺少依赖：** 启动 DashScope Web UI 时保留 `--extra web`；安装命令也使用相同参数。
- **端口被占用：** 增加 `--port 8766`，再打开对应地址。
- **配置或认证失败：** 检查本地 `.env` 中的 Key、端点和模型；缺少凭证时服务会报错，修正配置后再启动。
- **看不到 CLI 历史：** 检查两端的启动目录和 `--db`，再刷新会话列表。
- **修改模型后仍显示旧值：** 重启服务；若 Shell 导出了模型变量，还要检查它是否覆盖 `.env`。
- **旧会话无法继续：** 检查是否已有运行中任务，或模型、端点、工作区与会话绑定不匹配。切换默认模型后请新建会话。

下一章：[Web 与 CLI 的模块交互](11-web-architecture.md)。
