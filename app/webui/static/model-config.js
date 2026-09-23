"use strict";

// Key 只存在于密码输入框和一次 PUT 正文；读取接口永远不会回传密钥。
window.ModelConfigView = class ModelConfigView {
  constructor(api, onSaved) {
    this.api = api;
    this.onSaved = onSaved;
    this.data = null;
    this.active = "deepseek";
    this.loading = false;
    this.saving = false;
    this.busy = false;
    this.clearRequested = false;
    this.message = "";
    this.provider = document.querySelector("#model-config-provider");
    this.models = document.querySelector("#model-config-models");
    this.key = document.querySelector("#model-config-key");
    this.status = document.querySelector("#model-config-status");
    this.saveButton = document.querySelector("#model-config-save");
    this.clearButton = document.querySelector("#model-config-clear-key");
    this.reloadButton = document.querySelector("#model-config-reload");
    this.provider.addEventListener("change", () => {
      if (this.dirty()) {
        this.provider.value = this.active;
        this.message = "请先保存或重新加载当前供应商配置。";
        this.refreshStatus();
        return;
      }
      this.active = this.provider.value;
      this.render();
    });
    this.models.addEventListener("input", () => this.refreshStatus());
    this.key.addEventListener("input", () => {
      if (this.key.value) this.clearRequested = false;
      this.refreshStatus();
    });
    this.clearButton.addEventListener("click", () => {
      this.clearRequested = !this.clearRequested;
      this.key.value = "";
      this.refreshStatus();
    });
    this.reloadButton.addEventListener("click", () => this.load(true));
    document.querySelector("#model-config-form").addEventListener("submit", (event) => {
      event.preventDefault();
      this.save();
    });
    this.refreshStatus();
  }

  savedModels() {
    return this.data?.providers[this.active]?.models || [];
  }

  draftModels() {
    return this.models.value.split(/\r?\n/).map((model) => model.trim()).filter(Boolean);
  }

  dirty() {
    return !!this.data && (
      JSON.stringify(this.draftModels()) !== JSON.stringify(this.savedModels())
      || !!this.key.value || this.clearRequested
    );
  }

  setBusy(busy) {
    this.busy = busy;
    this.refreshStatus();
  }

  refreshStatus() {
    const disabled = this.loading || this.saving || this.busy || !this.data;
    this.provider.disabled = disabled;
    this.models.disabled = disabled;
    this.key.disabled = disabled || this.clearRequested;
    this.saveButton.disabled = disabled || !this.dirty();
    this.clearButton.disabled = disabled || (!this.data.providers[this.active].api_key_configured && !this.clearRequested);
    this.reloadButton.disabled = this.loading || this.saving || this.busy;
    this.clearButton.textContent = this.clearRequested ? "撤销删除 Key" : "删除已保存的 Key";
    this.status.textContent = this.loading ? "正在加载…" : this.saving ? "正在保存…" :
      this.message || (this.clearRequested ? "保存后将删除此供应商的 Key。" :
        this.data ? `${this.data.providers[this.active].api_key_configured ? "Key 已配置" : "Key 未配置"}${this.dirty() ? " · 尚未保存" : ""}` : "配置尚未加载");
  }

  render() {
    if (!this.data) return this.refreshStatus();
    this.provider.value = this.active;
    this.models.value = this.savedModels().join("\n");
    this.key.value = "";
    this.clearRequested = false;
    this.message = "";
    this.refreshStatus();
  }

  async load(force = false) {
    if (!force && this.dirty()) return;
    this.loading = true;
    this.message = "";
    this.refreshStatus();
    try {
      this.data = await (await this.api("/model-config")).json();
      this.render();
    } catch (error) {
      this.message = error.message;
    } finally {
      this.loading = false;
      this.refreshStatus();
    }
  }

  async save() {
    if (!this.data || this.busy || this.saving || !this.dirty()) return;
    const models = this.draftModels();
    if (models.length < 1 || models.length > 50) {
      this.message = "候选模型需为 1 至 50 个。";
      this.refreshStatus();
      return;
    }
    const action = this.key.value ? "set" : this.clearRequested ? "clear" : "keep";
    const payload = {
      expected_revision: this.data.revision,
      provider: this.active,
      models,
      api_key_action: action,
    };
    if (action === "set") payload.api_key = this.key.value;
    this.saving = true;
    this.message = "";
    this.refreshStatus();
    try {
      const updated = await (await this.api("/model-config", {
        method: "PUT", body: JSON.stringify(payload),
      })).json();
      this.data = { revision: updated.revision, providers: updated.providers };
      this.render();
      await this.onSaved(updated);
      this.message = "模型配置已保存，下一次请求生效。";
    } catch (error) {
      this.message = error.message;
    } finally {
      // 即使保存失败，也不要在页面内长期保留刚输入的密钥。
      this.key.value = "";
      this.saving = false;
      this.refreshStatus();
    }
  }
};
