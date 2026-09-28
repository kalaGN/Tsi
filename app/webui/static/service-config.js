"use strict";

// 仅按服务端的已知服务目录生成输入项，不允许页面自定义任意外部端点。
window.ServiceConfigView = class ServiceConfigView {
  constructor(api) {
    this.api = api;
    this.data = null;
    this.fields = new Map();
    this.busy = false;
    this.loading = false;
    this.saving = false;
    this.message = "";
    this.status = document.querySelector("#service-config-status");
    this.list = document.querySelector("#service-config-list");
    this.reloadButton = document.querySelector("#service-config-reload");
    this.reloadButton.addEventListener("click", () => this.load(true));
    this.refresh();
  }

  dirty() {
    return [...this.fields.values()].some((row) => row.input.value || row.clearRequested);
  }

  setBusy(busy) {
    this.busy = busy;
    this.refresh();
  }

  refresh() {
    const disabled = this.busy || this.loading || this.saving || !this.data;
    this.status.textContent = this.loading ? "正在加载…" : this.saving ? "正在保存…" :
      this.message || (this.data ? "配置仅保存在本机。" : "配置尚未加载。");
    this.reloadButton.disabled = this.busy || this.loading || this.saving;
    for (const row of this.fields.values()) {
      const changed = !!row.input.value || row.clearRequested;
      row.input.disabled = disabled || row.clearRequested;
      row.save.disabled = disabled || !changed;
      row.clear.disabled = disabled || (!row.configured && !row.clearRequested);
      row.clear.textContent = row.clearRequested ? "撤销删除" : "删除已保存的 Key";
      row.status.textContent = row.clearRequested ? "保存后删除已保存的 Key。" :
        `${row.configured ? "Key 已配置" : "Key 未配置"}${changed ? " · 尚未保存" : ""}`;
    }
  }

  renderFields() {
    this.fields.clear();
    this.list.replaceChildren();
    for (const service of this.data.services) {
      const card = document.createElement("section");
      card.className = "settings-card";
      const title = document.createElement("h3");
      title.textContent = service.name;
      card.append(title);
      for (const field of service.fields) {
        const key = `${service.id}:${field.id}`;
        const label = document.createElement("label");
        label.className = "setting-control";
        const caption = document.createElement("span");
        caption.textContent = `${field.name}（留空则保留现有密钥）`;
        const input = document.createElement("input");
        input.type = "password";
        input.autocomplete = "new-password";
        input.spellcheck = false;
        input.placeholder = "输入新密钥";
        label.append(caption, input);
        const status = document.createElement("p");
        status.className = "budget-status";
        status.setAttribute("role", "status");
        const actions = document.createElement("div");
        actions.className = "budget-actions";
        const clear = document.createElement("button");
        clear.type = "button";
        clear.className = "secondary-button";
        const save = document.createElement("button");
        save.type = "button";
        save.className = "primary-action-button";
        save.textContent = "保存配置";
        actions.append(clear, save);
        card.append(label, status, actions);
        const row = { input, status, clear, save, configured: field.configured, clearRequested: false };
        this.fields.set(key, row);
        input.addEventListener("input", () => {
          if (input.value) row.clearRequested = false;
          this.message = "";
          this.refresh();
        });
        clear.addEventListener("click", () => {
          row.clearRequested = !row.clearRequested;
          input.value = "";
          this.message = "";
          this.refresh();
        });
        save.addEventListener("click", () => this.save(service.id, field.id, row));
      }
      this.list.append(card);
    }
    this.refresh();
  }

  async load(force = false) {
    if (!force && this.dirty()) return;
    this.loading = true;
    this.message = "";
    this.refresh();
    try {
      this.data = await (await this.api("/service-config")).json();
      this.renderFields();
    } catch (error) {
      this.message = error.message;
    } finally {
      this.loading = false;
      this.refresh();
    }
  }

  async save(serviceId, fieldId, row) {
    if (!this.data || this.busy || this.saving) return;
    const action = row.clearRequested ? "clear" : "set";
    const payload = { expected_revision: this.data.revision, field_id: fieldId, action };
    if (action === "set") payload.value = row.input.value;
    this.saving = true;
    this.message = "";
    this.refresh();
    try {
      this.data = await (await this.api(`/service-config/${encodeURIComponent(serviceId)}`, {
        method: "PUT", body: JSON.stringify(payload),
      })).json();
      this.renderFields();
      this.message = "服务配置已保存，下次调用生效。";
    } catch (error) {
      this.message = error.message;
    } finally {
      // 页面不长期持有刚输入的密钥，失败也需重新输入。
      row.input.value = "";
      this.saving = false;
      this.refresh();
    }
  }
};
