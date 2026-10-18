"use strict";

// Presentation state only. The application service owns runs and SQLite owns history.
const state = {
  config: null, sessions: [], sessionId: null, runs: [], nextBefore: null,
  selectedRun: null, events: new Map(), stream: null, pending: false, loading: false,
  compatible: true, version: 0,
};
const $ = (id) => document.getElementById(id);
const welcome = $("conversation").firstElementChild.cloneNode(true);
const statuses = {
  idle: "新会话", running: "执行中", completed: "循环已结束", verification_failed: "验收检查未通过",
  step_limit: "达到步数上限", time_limit: "达到时间上限", context_limit: "上下文超限",
  model_error: "模型请求失败", protocol_error: "协议错误", cancelled: "已取消",
  internal_error: "内部错误", abandoned: "已放弃",
};
const providerNames = { openai: "OpenAI", dashscope: "DashScope" };
const errors = {
  session_busy: "这个会话已有任务在执行。请刷新记录。",
  session_binding_mismatch: "此会话的模型或工作区与当前服务不同。请新建会话。",
  server_busy: "服务已有 4 个后台任务。请稍后重试。",
  same_origin_required: "请求来源不匹配。请从本地工作台重新打开页面。",
  invalid_request: "消息格式不正确，或超过 16000 个字符。",
  empty_question: "请输入任务消息。", model_configuration_error: "模型配置不可用，请检查服务端配置。",
};

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function error(message) {
  $("error").textContent = errors[message] || message;
  $("error").hidden = !message;
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options, headers: { "Content-Type": "application/json", "X-Learn-Agent": "web", ...options.headers },
  });
  let data;
  try { data = await response.json(); }
  catch { throw new Error(`服务返回了无效响应（HTTP ${response.status}）。`); }
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${response.status}`);
  return data;
}
function controls() {
  const running = state.runs.some((run) => run.status === "running");
  $("send").disabled = state.pending || state.loading || running || !state.compatible || !state.config;
  $("new-session").disabled = state.pending || !state.config;
  $("question").disabled = !state.compatible;
  $("composer-hint").textContent = !state.compatible ? "模型或工作区不同，请新建会话" :
    running ? "任务正在执行，可在右侧查看进度" : "Enter 发送 · Shift + Enter 换行";
}
function closeStream() {
  if (state.stream) state.stream.close();
  state.stream = null;
}
function sidebar() {
  $("sessions").replaceChildren();
  if (!state.sessions.length) $("sessions").append(node("p", "empty-sessions", "还没有会话。发送任务即可开始。"));
  for (const session of state.sessions) {
    const button = node("button", `session-item${session.id === state.sessionId ? " active" : ""}`);
    button.type = "button";
    button.append(node("strong", "", session.title));
    const meta = node("small");
    meta.append(node("span", "", providerNames[session.model_key.split(":")[0]] || "Model"),
      node("span", "", statuses[session.status] || session.status));
    button.append(meta);
    button.disabled = state.pending;
    button.addEventListener("click", () => loadSession(session.id).catch((e) => error(e.message)));
    $("sessions").append(button);
  }
}
function message(role, text, label = role === "user" ? "你" : "模型说明（不作为验收结论）") {
  const box = node("div", `message ${role}`);
  box.append(node("div", "message-label", label),
    node("div", "message-text", text));
  return box;
}
function outcomeCard(result) {
  const outcome = result.outcome || { status: "unverified", summary: "缺少系统验收结论", detail: "请重启后端服务以读取系统结论。" };
  const card = node("section", `outcome-card ${outcome.status}`);
  card.append(node("strong", "outcome-title", `系统结论：${outcome.summary}`),
    node("p", "", outcome.detail));
  if (result.tool_evidence?.length) {
    const evidence = node("ul", "result-evidence");
    for (const item of result.tool_evidence) {
      const target = item.path ? ` ${item.path}` : "";
      evidence.append(node("li", "", `${item.tool}${target} → ${item.code}`));
    }
    card.append(node("small", "", "已记录的实际工具结果"), evidence);
  }
  for (const check of result.verification.checks || []) {
    card.append(node("p", "check-evidence",
      `${check.target}：${check.status === "passed" ? "通过" : "未通过"} · ${check.detail}`));
    if (check.evidence.sha256) card.append(node("small", "evidence-hash", `sha256: ${check.evidence.sha256}`));
  }
  return card;
}
function renderChat() {
  const container = $("conversation");
  container.replaceChildren();
  if (!state.runs.length) {
    const element = welcome.cloneNode(true);
    element.querySelector("button").addEventListener("click", example);
    container.append(element);
  }
  for (const run of state.runs) {
    const block = node("article", "run-block");
    const calls = new Map();
    for (const item of run.messages) {
      if (item.role === "user") {
        block.append(message("user", item.text));
        if (run.result) block.append(outcomeCard(run.result));
      }
      if (item.role === "assistant") {
        for (const call of item.calls) calls.set(call.id, call);
        if (item.text) block.append(message("assistant", item.text,
          item.calls.length ? "模型进度说明" : "模型说明（不作为验收结论）"));
      }
      if (item.role === "tool" && item.result) {
        const detail = node("details", "observation");
        const call = calls.get(item.call_id);
        detail.append(node("summary", "", `${call?.name || "工具"} · ${item.result.code}`));
        detail.append(node("pre", "", JSON.stringify({
          call_id: item.call_id, arguments: call?.arguments, result: item.result,
        }, null, 2)));
        block.append(detail);
      }
    }
    if (run.status === "running") block.append(message("assistant", "任务执行中。你可以在右侧查看模型与工具进度。", "系统进度"));
    const footer = node("div", "run-footer");
    footer.append(node("span", run.result && run.result.outcome?.status !== "passed" ? "attention" : "",
      run.result ? (run.result.outcome?.summary || "缺少系统验收结论") : (statuses[run.status] || run.status)));
    if (run.result) {
      const verification = { passed: "指定检查通过", failed: "指定检查失败", unchecked: "未验证" };
      footer.append(node("span", "", `· ${run.result.steps} Steps`),
        node("span", "", `· ${verification[run.result.verification.status]}`));
    }
    const inspect = node("button", "text-button", "查看过程 ↗");
    inspect.type = "button";
    inspect.addEventListener("click", () => selectRun(run));
    footer.append(inspect);
    block.append(footer);
    container.append(block);
  }
  $("older").hidden = !state.nextBefore;
  controls();
  const conclusion = container.lastElementChild?.querySelector(".outcome-card");
  if (conclusion) conclusion.scrollIntoView({ block: "nearest" });
  else container.scrollTop = container.scrollHeight;
}
function eventLabel(event) {
  const d = event.data;
  switch (event.name) {
    case "run.started": return ["任务已开始", "已保存用户请求", ""];
    case "model.started": return [`Step ${d.step} · 请求模型`, `输入 ${d.input_bytes} bytes · 省略 ${d.omitted_turns} 个旧轮次`, ""];
    case "model.finished": return [`Step ${d.step} · 模型返回`, d.calls ? `提出 ${d.calls} 个工具调用` : "返回模型说明", ""];
    case "tool.started": return [d.tool, "参数与权限检查 → 执行", "tool"];
    case "tool.finished": return [d.tool, `${d.ok ? "成功" : "未成功"} · ${d.code}`, d.ok ? "tool" : "error"];
    case "run.finished": return [statuses[d.status] || d.status, `验证：${d.verification}`, d.status === "completed" ? "" : "error"];
    case "model.error": return ["模型错误", d.code, "error"];
    case "run.error": return ["执行错误", d.error_type, "error"];
    default: return [event.name, JSON.stringify(d), ""];
  }
}
function renderProgress() {
  $("progress").replaceChildren();
  for (const event of state.events.values()) {
    const [title, description, kind] = eventLabel(event);
    const item = node("li", `progress-event ${kind}`);
    item.append(node("strong", "", title), node("small", "", description),
      node("time", "", new Date(event.at).toLocaleTimeString("zh-CN", { hour12: false })));
    $("progress").append(item);
  }
  $("raw-events").textContent = JSON.stringify([...state.events.values()], null, 2);
  $("progress").scrollTop = $("progress").scrollHeight;
}
function selectRun(run, preserve = false) {
  closeStream();
  if (!preserve || state.selectedRun !== run.id) state.events.clear();
  state.selectedRun = run.id;
  $("run-identity").textContent = `Run ${run.id}`;
  $("stream-state").textContent = run.status === "running" ? "执行中" : "读取记录";
  renderProgress();
  const sessionId = state.sessionId;
  const source = new EventSource(`/api/runs/${encodeURIComponent(run.id)}/events`);
  state.stream = source;
  source.addEventListener("progress", (event) => {
    if (state.stream !== source) return;
    const data = JSON.parse(event.data);
    state.events.set(data.seq, data);
    renderProgress();
  });
  source.addEventListener("end", async () => {
    source.close();
    if (state.stream !== source) return;
    state.stream = null;
    $("stream-state").textContent = "已读完";
    if (run.status === "running" && state.sessionId === sessionId) {
      try { await loadSession(sessionId, true); await refreshSessions(); }
      catch (e) { error(e.message); }
    }
  });
  source.onerror = () => {
    if (state.stream === source) $("stream-state").textContent = "重新连接";
  };
}
async function refreshSessions() {
  state.sessions = (await api("/api/sessions")).sessions;
  sidebar();
}
async function loadSession(id, preserve = false) {
  const version = ++state.version;
  state.loading = true;
  closeStream();
  if (state.sessionId !== id) {
    state.runs = []; state.events.clear(); state.selectedRun = null;
  }
  state.sessionId = id;
  localStorage.setItem("learn-agent-session", id);
  controls(); sidebar();
  try {
    const data = await api(`/api/sessions/${encodeURIComponent(id)}`);
    if (version !== state.version) return;
    state.runs = data.runs;
    state.nextBefore = data.next_before;
    state.compatible = data.compatible;
    $("session-title").textContent = state.sessions.find((s) => s.id === id)?.title || "新会话";
    $("session-identity").textContent = `Session ${id}`;
    renderChat();
    const run = (preserve && data.runs.find((r) => r.id === state.selectedRun)) || data.runs.at(-1);
    if (run) selectRun(run, preserve);
    else {
      state.events.clear(); state.selectedRun = null; renderProgress();
      $("run-identity").textContent = "发送消息后，事件将在这里出现。";
      $("stream-state").textContent = "待命";
    }
  } finally {
    if (version === state.version) { state.loading = false; controls(); }
  }
}
async function createSession() {
  const session = await api("/api/sessions", { method: "POST", body: "{}" });
  await refreshSessions();
  await loadSession(session.id);
  return session.id;
}
function example() {
  $("question").value = "读取 notes.txt，创建 report.md";
  $("question").focus();
}
$("example-task").addEventListener("click", example);
$("new-session").addEventListener("click", async () => {
  if (state.pending) return;
  state.pending = true; controls(); error("");
  try { await createSession(); $("question").focus(); }
  catch (e) { error(e.message); }
  finally { state.pending = false; controls(); sidebar(); }
});
$("refresh").addEventListener("click", async () => {
  error("");
  try { await refreshSessions(); if (state.sessionId) await loadSession(state.sessionId, true); }
  catch (e) { error(e.message); }
});
$("older").addEventListener("click", async () => {
  const id = state.sessionId;
  const cursor = state.nextBefore;
  if (!id || !cursor) return;
  try {
    const data = await api(`/api/sessions/${encodeURIComponent(id)}?before=${cursor}`);
    if (state.sessionId !== id) return;
    const ids = new Set(state.runs.map((r) => r.id));
    state.runs = [...data.runs.filter((r) => !ids.has(r.id)), ...state.runs];
    state.nextBefore = data.next_before;
    renderChat(); $("conversation").scrollTop = 0;
  } catch (e) { error(e.message); }
});
$("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = $("question").value.trim();
  if (!question || $("send").disabled) return;
  state.pending = true; controls(); sidebar(); error("");
  try {
    const id = state.sessionId || await createSession();
    await api(`/api/sessions/${encodeURIComponent(id)}/runs`, { method: "POST", body: JSON.stringify({ question }) });
    $("question").value = "";
    await refreshSessions();
    if (state.sessionId === id) await loadSession(id);
  } catch (e) { error(e.message); }
  finally { state.pending = false; controls(); sidebar(); }
});
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault(); $("chat-form").requestSubmit();
  }
});
async function boot() {
  try {
    const [config, history] = await Promise.all([api("/api/config"), api("/api/sessions")]);
    state.config = config; state.sessions = history.sessions;
    $("model-badge").textContent = `${providerNames[config.provider] || "Model"} · ${config.model}`;
    $("permission").textContent = config.allow_write ? "可新建文件" : "只读工作区";
    $("workspace").textContent = config.workspace;
    $("workspace").title = config.workspace;
    $("mode-notice").textContent = `模型请求由 Python 后端发出。每个任务最多 ${config.max_steps} 次模型调用。密钥保留在服务端。`;
    sidebar(); controls();
    const saved = localStorage.getItem("learn-agent-session");
    const session = state.sessions.find((s) => s.id === saved) || state.sessions[0];
    if (session) await loadSession(session.id);
  } catch (e) { error(`无法连接本地服务：${e.message}`); }
}
controls();
boot();
