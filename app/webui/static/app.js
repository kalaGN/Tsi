"use strict";

const state = {
  busy: false,
  startedAt: 0,
  timer: null,
  streamNode: null,
  models: [],
  preferences: {
    theme: "system",
    density: "comfortable",
    sendKey: "enter",
  },
};

const PREFERENCES_KEY = "tsi-web-preferences";

const $ = (selector) => document.querySelector(selector);
const messages = $("#messages");
const prompt = $("#prompt");
const sendButton = $("#send");
const stopButton = $("#stop");

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { toast.hidden = true; }, 3200);
}

async function api(path, options = {}) {
  const response = await fetch(`/ui/api${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = `请求失败（${response.status}）`;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response;
}

function setConnected(ok, text) {
  const dot = $("#connection-dot");
  dot.className = ok ? "connected" : "error";
  $("#connection-text").textContent = text;
}

function autoSizeInput() {
  prompt.style.height = "auto";
  prompt.style.height = `${Math.min(prompt.scrollHeight, 180)}px`;
}

function loadPreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem(PREFERENCES_KEY) || "{}");
    const legacyTheme = localStorage.getItem("tsi-theme");
    if (["system", "light", "dark"].includes(saved.theme)) state.preferences.theme = saved.theme;
    else if (["light", "dark"].includes(legacyTheme)) state.preferences.theme = legacyTheme;
    if (["comfortable", "compact"].includes(saved.density)) state.preferences.density = saved.density;
    if (["enter", "modifier-enter"].includes(saved.sendKey)) state.preferences.sendKey = saved.sendKey;
  } catch (_) {
    localStorage.removeItem(PREFERENCES_KEY);
  }
}

function applyPreferences() {
  const { theme, density, sendKey } = state.preferences;
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  document.documentElement.dataset.density = density;
  $("#theme-setting").value = theme;
  $("#density-setting").value = density;
  $("#send-key-setting").value = sendKey;
  $("#send-hint").textContent = sendKey === "enter"
    ? "Enter 发送 · Shift+Enter 换行"
    : "⌘/Ctrl+Enter 发送 · Enter 换行";
}

function savePreference(name, value) {
  state.preferences[name] = value;
  localStorage.setItem(PREFERENCES_KEY, JSON.stringify(state.preferences));
  applyPreferences();
}

function setBusy(busy) {
  state.busy = busy;
  prompt.disabled = busy;
  sendButton.hidden = busy;
  stopButton.hidden = !busy;
  $("#activity-strip").hidden = !busy;
  if (busy) {
    state.startedAt = performance.now();
    state.timer = window.setInterval(() => {
      $("#elapsed-time").textContent = `${((performance.now() - state.startedAt) / 1000).toFixed(1)}s`;
    }, 100);
  } else {
    window.clearInterval(state.timer);
    state.timer = null;
  }
}

function scrollToBottom() {
  messages.scrollTop = messages.scrollHeight;
}

function textBlock(text) {
  const fragment = document.createDocumentFragment();
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (line.startsWith("```")) {
      const code = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith("```")) code.push(lines[index++]);
      const pre = document.createElement("pre");
      const node = document.createElement("code");
      node.textContent = code.join("\n");
      pre.append(node);
      fragment.append(pre);
    } else if (/^#{1,3}\s/.test(line)) {
      const level = line.match(/^#+/)[0].length;
      const heading = document.createElement(`h${level}`);
      heading.textContent = line.replace(/^#{1,3}\s+/, "");
      fragment.append(heading);
    } else if (/^[-*]\s+/.test(line)) {
      const list = document.createElement("ul");
      while (index < lines.length && /^[-*]\s+/.test(lines[index])) {
        const item = document.createElement("li");
        item.textContent = lines[index].replace(/^[-*]\s+/, "");
        list.append(item);
        index += 1;
      }
      fragment.append(list);
      continue;
    } else if (/^\d+\.\s+/.test(line)) {
      const list = document.createElement("ol");
      while (index < lines.length && /^\d+\.\s+/.test(lines[index])) {
        const item = document.createElement("li");
        item.textContent = lines[index].replace(/^\d+\.\s+/, "");
        list.append(item);
        index += 1;
      }
      fragment.append(list);
      continue;
    } else if (line.trim()) {
      const paragraph = document.createElement("p");
      paragraph.textContent = line;
      fragment.append(paragraph);
    }
    index += 1;
  }
  return fragment;
}

function addMessage(role, content, { streaming = false, error = false } = {}) {
  $("#welcome")?.remove();
  const wrapper = document.createElement("article");
  wrapper.className = `message ${role}${error ? " error" : ""}`;
  const label = document.createElement("div");
  label.className = "message-role";
  label.textContent = role === "user" ? "你" : error ? "错误" : "Tsi";
  const body = document.createElement("div");
  body.className = `message-content${streaming ? " stream-caret" : ""}`;
  if (role === "assistant" && !streaming && !error) body.append(textBlock(content));
  else body.textContent = content;
  wrapper.append(label, body);
  messages.append(wrapper);
  scrollToBottom();
  return body;
}

function addActivity(title, status, detail = "") {
  const list = $("#activity-list");
  list.querySelector(".empty-state")?.remove();
  const card = document.createElement("article");
  card.className = "activity-card";
  const header = document.createElement("header");
  const name = document.createElement("strong");
  name.textContent = title;
  const stateNode = document.createElement("span");
  stateNode.textContent = status;
  header.append(name, stateNode);
  card.append(header);
  if (detail) {
    const small = document.createElement("small");
    small.textContent = detail;
    card.append(small);
  }
  list.prepend(card);
}

function finishStreamAsMarkdown(finalText) {
  if (!state.streamNode) return;
  state.streamNode.classList.remove("stream-caret");
  state.streamNode.replaceChildren(textBlock(finalText));
  state.streamNode = null;
}

async function sendMessage() {
  const input = prompt.value;
  if (state.busy || !input.trim()) return;
  addMessage("user", input);
  $("#conversation-preview").textContent = input.trim().slice(0, 45);
  prompt.value = "";
  autoSizeInput();
  state.streamNode = addMessage("assistant", "", { streaming: true });
  $("#activity-list").replaceChildren();
  addActivity("模型请求", "进行中");
  $("#activity-text").textContent = "思考中";
  setBusy(true);

  try {
    const response = await api("/chat", { method: "POST", body: JSON.stringify({ input }) });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) if (line.trim()) handleEvent(JSON.parse(line));
      if (done) break;
    }
    if (buffer.trim()) handleEvent(JSON.parse(buffer));
  } catch (error) {
    if (state.streamNode) state.streamNode.textContent = "";
    addMessage("assistant", error.message, { error: true });
    state.streamNode?.closest(".message")?.remove();
    state.streamNode = null;
    addActivity("模型请求", "失败", error.message);
  } finally {
    setBusy(false);
    prompt.disabled = false;
    prompt.focus();
  }
}

function handleEvent(event) {
  if (event.type === "text_delta" && state.streamNode) {
    state.streamNode.textContent += event.text;
    scrollToBottom();
  } else if (event.type === "text_reset" && state.streamNode) {
    state.streamNode.textContent = "";
    $("#activity-text").textContent = "正在调用工具";
  } else if (event.type === "tool_finished") {
    addActivity(event.tool, event.status === "success" ? "已完成" : "失败");
    $("#activity-text").textContent = `工具：${event.tool}`;
  } else if (event.type === "completed") {
    finishStreamAsMarkdown(event.output_text);
    $("#elapsed-time").textContent = `${(event.elapsed_ms / 1000).toFixed(1)}s`;
    $("#context-text").textContent = `上下文 ${event.context_percent}%`;
    $("#usage-text").textContent = event.token_usage
      ? `Token：${event.token_usage.input} + ${event.token_usage.output} = ${event.token_usage.total}`
      : "Token：不可用";
    addActivity("模型请求", "已完成", `${(event.elapsed_ms / 1000).toFixed(1)}s`);
  } else if (event.type === "failed") {
    state.streamNode?.closest(".message")?.remove();
    state.streamNode = null;
    addMessage("assistant", event.message || "请求失败", { error: true });
    addActivity("模型请求", "失败", event.message || "请求失败");
  } else if (event.type === "cancelled") {
    state.streamNode?.closest(".message")?.remove();
    state.streamNode = null;
    addActivity("模型请求", "已取消");
    showToast("已取消当前请求");
  }
}

async function loadFiles() {
  const list = $("#file-list");
  list.textContent = "加载中…";
  try {
    const response = await api("/files");
    const data = await response.json();
    list.replaceChildren();
    for (const item of data.items) {
      const button = document.createElement("button");
      button.className = `file-entry ${item.type}`;
      const icon = document.createElement("span");
      icon.textContent = item.type === "directory" ? "⌄" : "·";
      const label = document.createElement("span");
      label.textContent = item.path;
      button.append(icon, label);
      if (item.type === "file") button.addEventListener("click", () => previewFile(item.path));
      list.append(button);
    }
  } catch (error) {
    list.textContent = error.message;
  }
}

async function previewFile(path) {
  try {
    const response = await api(`/files/preview?path=${encodeURIComponent(path)}`, { headers: {} });
    const data = await response.json();
    $("#preview-name").textContent = data.path;
    $("#preview-content").textContent = data.content;
    $("#file-preview").hidden = false;
  } catch (error) { showToast(error.message); }
}

function populateModels(models, runtime) {
  state.models = models;
  for (const select of [$("#model-select"), $("#settings-model-select")]) {
    select.replaceChildren();
    for (const [index, item] of models.entries()) {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = `${item.provider === "deepseek" ? "DeepSeek" : "Aliyun"} · ${item.model}${item.api_key_configured ? "" : "（未配置）"}`;
      option.disabled = !item.api_key_configured;
      option.selected = item.provider === runtime.provider && item.model === runtime.model;
      select.append(option);
    }
    select.dataset.current = select.value;
  }
}

async function selectModel(event) {
  const source = event.target;
  const previous = source.dataset.current;
  const item = state.models[Number(source.value)];
  if (!item) {
    source.value = previous;
    return;
  }
  try {
    const response = await api("/model", {
      method: "POST",
      body: JSON.stringify({ provider: item.provider, model: item.model }),
    });
    const data = await response.json();
    for (const select of [$("#model-select"), $("#settings-model-select")]) {
      select.value = source.value;
      select.dataset.current = source.value;
    }
    setConnected(true, `${item.provider} · ${item.model}`);
    if (data.warning) showToast(data.warning);
  } catch (error) {
    source.value = previous;
    showToast(error.message);
  }
}

async function bootstrap() {
  try {
    const response = await api("/bootstrap");
    const data = await response.json();
    $("#workspace-name").textContent = data.workspace_name;
    $("#workspace-path").textContent = data.workspace_path;
    $("#agents-state").textContent = data.startup_warning ? "警告" : data.system_prompt_loaded ? "已加载" : "无";
    $("#context-text").textContent = `上下文 ${data.context_percent}%`;
    populateModels(data.models, data.runtime);
    for (const message of data.messages) addMessage(message.role, message.content);
    setConnected(data.runtime.api_key_configured, data.runtime.api_key_configured ? "本地服务已连接" : "API Key 未配置");
    sendButton.disabled = !data.runtime.api_key_configured;
    if (data.startup_warning) showToast(data.startup_warning);
    await loadFiles();
  } catch (error) {
    setConnected(false, "连接失败");
    sendButton.disabled = true;
    showToast(error.message);
  }
}

prompt.addEventListener("input", autoSizeInput);
prompt.addEventListener("keydown", (event) => {
  const enterToSend = state.preferences.sendKey === "enter" && !event.shiftKey;
  const modifierToSend = state.preferences.sendKey === "modifier-enter" && (event.metaKey || event.ctrlKey);
  if (event.key === "Enter" && (enterToSend || modifierToSend)) {
    event.preventDefault();
    sendMessage();
  }
});
sendButton.addEventListener("click", sendMessage);
stopButton.addEventListener("click", async () => {
  try { await api("/cancel", { method: "POST", body: "{}" }); } catch (error) { showToast(error.message); }
});
$("#new-chat").addEventListener("click", async () => {
  if (state.busy || !window.confirm("清空当前 Web 会话并开始新对话？")) return;
  try {
    await api("/clear", { method: "POST", body: "{}" });
    messages.replaceChildren();
    window.location.reload();
  } catch (error) { showToast(error.message); }
});
$("#model-select").addEventListener("change", selectModel);
$("#settings-model-select").addEventListener("change", selectModel);
$("#toggle-inspector").addEventListener("click", () => $(".app-shell").classList.toggle("inspector-closed"));
$("#open-sidebar").addEventListener("click", () => $(".app-shell").classList.add("sidebar-open"));
$("#close-sidebar").addEventListener("click", () => $(".app-shell").classList.remove("sidebar-open"));
$("#refresh-files").addEventListener("click", loadFiles);
$("#close-preview").addEventListener("click", () => { $("#file-preview").hidden = true; });
document.querySelectorAll(".inspector-tabs button").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".inspector-tabs button").forEach((item) => item.classList.toggle("active", item === button));
  document.querySelectorAll(".inspector-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `${button.dataset.tab}-panel`));
}));
document.querySelectorAll("[data-prompt]").forEach((button) => button.addEventListener("click", () => {
  prompt.value = button.dataset.prompt;
  autoSizeInput();
  prompt.focus();
}));
$("#open-settings").addEventListener("click", () => $("#settings-dialog").showModal());
$("#theme-setting").addEventListener("change", (event) => savePreference("theme", event.target.value));
$("#density-setting").addEventListener("change", (event) => savePreference("density", event.target.value));
$("#send-key-setting").addEventListener("change", (event) => savePreference("sendKey", event.target.value));
$("#reset-settings").addEventListener("click", () => {
  state.preferences = { theme: "system", density: "comfortable", sendKey: "enter" };
  localStorage.removeItem(PREFERENCES_KEY);
  localStorage.removeItem("tsi-theme");
  applyPreferences();
  showToast("已恢复默认设置");
});
$("#settings-dialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});

loadPreferences();
applyPreferences();
bootstrap();
