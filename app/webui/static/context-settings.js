"use strict";

// 设置草稿留在页面内存；只在显式保存时写入后端，不将模型配置写到 localStorage。
window.ContextSettingsView = class ContextSettingsView {
  static fields = {
    model: [
      ["context_window_tokens", "上下文窗口", "Token", false, "按模型实际能力填写，不代表上游已验证支持。"],
      ["max_output_tokens", "最大回复长度", "Token", false, "为每次业务请求预留的最大输出。"],
      ["summary_output_tokens", "摘要输出上限", "Token", true, "只限制内部摘要，不影响最大回复长度。"],
      ["safety_margin_tokens", "安全余量", "Token", true, "补偿本地 Token 估算和协议模板误差。"],
    ],
    compaction: [
      ["trigger_percent", "触发占比", "%", false, "按可用输入预算计算，不是模型总窗口。"],
      ["target_percent", "压缩目标", "%", false, "必须小于触发占比；最近原文优先保留。"],
      ["recent_turns", "保留最近对话", "轮", false, "一轮包含用户和助手消息；硬超限时仍可能淘汰。"],
      ["summary_timeout_seconds", "摘要超时", "秒", true, "达到时限后取消摘要并降级。"],
      ["summary_cooldown_seconds", "失败冷却", "分钟", true, "冷却期间不重复调用摘要模型。"],
    ],
  };

  constructor(api, notify, onLockChange) {
    this.api = api;
    this.notify = notify;
    this.onLockChange = onLockChange;
    this.data = null;
    this.drafts = new Map();
    this.busy = false;
    this.saving = false;
    this.loading = false;
    this.generation = 0;
    this.forms = {};
    for (const scope of ["model", "compaction"]) this.buildForm(scope);
    this.render();
  }

  buildForm(scope) {
    const root = document.querySelector(`[data-budget-form="${scope}"]`);
    const heading = document.createElement("h3");
    heading.textContent = scope === "model" ? "模型预算" : "整理策略";
    root.append(heading);
    const form = document.createElement("form");
    const advanced = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "高级设置";
    advanced.append(summary);
    const inputs = {};
    for (const [key, title, unit, isAdvanced, help] of ContextSettingsView.fields[scope]) {
      const label = document.createElement("label");
      label.className = "budget-field";
      const copy = document.createElement("span");
      const name = document.createElement("strong");
      name.textContent = title;
      const description = document.createElement("small");
      description.textContent = help;
      const source = document.createElement("small");
      source.className = "budget-source";
      copy.append(name, description, source);
      const value = document.createElement("span");
      value.className = "budget-value";
      const input = document.createElement("input");
      input.type = "number";
      input.id = `context-setting-${key}`;
      input.name = key;
      input.step = "1";
      input.required = true;
      input.setAttribute("aria-label", title);
      const suffix = document.createElement("span");
      suffix.textContent = unit;
      value.append(input, suffix);
      label.append(copy, value);
      (isAdvanced ? advanced : form).append(label);
      inputs[key] = { input, source };
      input.addEventListener("input", () => this.edit(scope, key, input.value));
    }
    form.append(advanced);
    const hint = document.createElement("p");
    hint.className = "settings-note";
    const status = document.createElement("p");
    status.className = "budget-status";
    status.setAttribute("role", "status");
    const actions = document.createElement("div");
    actions.className = "budget-actions";
    const buttons = {};
    for (const [name, text] of [["reset", scope === "model" ? "恢复继承" : "恢复默认"], ["discard", "放弃修改"], ["save", "保存"]]) {
      const button = document.createElement("button");
      button.type = name === "save" ? "submit" : "button";
      button.className = name === "save" ? "primary-action-button" : "secondary-button";
      button.textContent = text;
      buttons[name] = button;
      actions.append(button);
    }
    buttons.reset.addEventListener("click", () => {
      this.draft(scope).values = {};
      this.render();
    });
    buttons.discard.addEventListener("click", () => {
      this.drafts.delete(this.key(scope));
      this.load();
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (form.reportValidity()) this.save(scope);
    });
    form.append(hint, status, actions);
    root.append(form);
    this.forms[scope] = { form, inputs, hint, status, ...buttons };
  }

  key(scope) {
    return scope === "model" ? `model:${this.data.provider}:${this.data.model}` : "compaction";
  }

  draft(scope) {
    const key = this.key(scope);
    if (!this.drafts.has(key)) {
      const saved = { ...this.data.overrides[scope] };
      this.drafts.set(key, { values: { ...saved }, saved, revision: this.data.revision });
    }
    return this.drafts.get(key);
  }

  dirty(scope) {
    if (!this.data) return false;
    const { values, saved } = this.draft(scope);
    return ContextSettingsView.fields[scope].some(([key]) => values[key] !== saved[key]);
  }

  get modelLocked() {
    return this.saving || this.dirty("model");
  }

  setBusy(busy) { this.busy = busy; this.render(); }

  edit(scope, key, text) {
    if (!this.data) return;
    const factor = key === "summary_cooldown_seconds" ? 60 : 1;
    const value = text === "" ? NaN : Number(text) * factor;
    const draft = this.draft(scope);
    if (value === this.data.baseline[key]) delete draft.values[key];
    else draft.values[key] = value;
    // 不重写正在输入的控件，保留负号/空值等中间态供浏览器验证。
    this.render(false);
  }

  async load() {
    const generation = ++this.generation;
    this.loading = true;
    this.render(false);
    try {
      const data = await (await this.api("/context-settings")).json();
      if (generation !== this.generation) return;
      // 没改动的旧草稿更新基线；未保存草稿保留旧 revision，防止覆盖其他页面。
      if (this.data) {
        for (const scope of ["model", "compaction"]) {
          if (!this.dirty(scope)) this.drafts.delete(this.key(scope));
        }
      }
      this.data = data;
      this.error = null;
    } catch (error) {
      if (generation !== this.generation) return;
      this.error = error.message;
    } finally {
      if (generation === this.generation) { this.loading = false; this.render(); }
    }
  }

  async save(scope) {
    if (!this.data || this.busy || this.saving || this.loading || !this.dirty(scope)) return;
    const draft = this.draft(scope);
    const payload = { expected_revision: draft.revision, scope, overrides: draft.values };
    if (scope === "model") Object.assign(payload, { provider: this.data.provider, model: this.data.model });
    this.saving = true;
    this.error = null;
    this.render(false);
    try {
      const data = await (await this.api("/context-settings", { method: "PUT", body: JSON.stringify(payload) })).json();
      this.drafts.delete(this.key(scope));
      // 本页面已知的成功写入不与另一范围的草稿冲突。
      for (const remaining of this.drafts.values()) {
        if (remaining.revision === draft.revision) remaining.revision = data.revision;
      }
      this.data = data;
      this.notify("已保存，下次请求生效");
    } catch (error) {
      this.error = error.message;
    } finally {
      this.saving = false;
      this.render();
    }
  }

  render(updateInputs = true) {
    const names = { settings: "页面覆盖", model_config: "模型环境配置", legacy_env: "窗口环境变量", default: "内置默认" };
    for (const scope of ["model", "compaction"]) {
      const view = this.forms[scope];
      if (!view) continue;
      const dirty = this.dirty(scope);
      for (const [key, controls] of Object.entries(view.inputs)) {
        controls.input.disabled = !this.data || this.saving;
        if (!this.data) continue;
        const draft = this.draft(scope);
        const overridden = Object.hasOwn(draft.values, key);
        const value = overridden ? draft.values[key] : this.data.baseline[key];
        const factor = key === "summary_cooldown_seconds" ? 60 : 1;
        controls.input.min = this.data.limits[key][0] / factor;
        controls.input.max = this.data.limits[key][1] / factor;
        if (updateInputs) controls.input.value = Number.isFinite(value) ? value / factor : "";
        controls.source.textContent = overridden ? (dirty ? "未保存覆盖" : "页面覆盖") : `继承：${names[this.data.baseline_sources[key]]}`;
      }
      view.save.disabled = !dirty || this.busy || this.saving || this.loading || Boolean(this.data?.warning);
      view.discard.disabled = !dirty || this.saving;
      view.reset.disabled = !this.data || this.saving;
      view.status.textContent = this.error || this.data?.warning || (this.saving ? "正在保存…" : this.loading ? "正在加载…" : dirty ? "未保存 · 刷新页面会丢失修改" : this.busy ? "请求运行中，可编辑，结束后保存" : "已保存 · 下次请求生效");
      if (this.data) {
        const values = { ...this.data.baseline, ...this.draft("model").values };
        const limit = values.context_window_tokens - values.max_output_tokens - values.safety_margin_tokens;
        view.hint.textContent = scope === "model" ? `${this.data.provider} · ${this.data.model} ｜ 可用输入预算 ${Number.isFinite(limit) ? limit.toLocaleString("zh-CN") : "—"} Token` : "不会删除完整历史；摘要会产生一次额外模型调用。恢复默认也需要点击保存。";
      }
    }
    this.onLockChange(this.modelLocked);
  }
};
