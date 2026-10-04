"use strict";

// 不接收异常 message/stack、URL 参数、聊天正文或模型内容。
window.TsiDiagnostics = (() => {
  let context = {};
  let started = 0;
  let sent = 0;
  let windowStarted = Date.now();
  let terminal = false;
  let generation = 0;
  let busyState = () => false;
  const terminalTypes = new Set(["completed", "failed", "cancelled", "preflight_clarify", "preflight_cancelled"]);
  const errors = new Set(["Error", "TypeError", "SyntaxError", "RangeError", "ReferenceError", "AbortError", "NetworkError"]);
  function report(event, extra = {}) {
    if (Date.now() - windowStarted > 60000) { sent = 0; windowStarted = Date.now(); }
    if (sent++ >= 30) return;
    const payload = { ...context, event, page_version: "1", elapsed_ms: Math.min(86400000, Math.max(0, performance.now() - started)), ...extra };
    try {
      // 旁路请求不经过业务 api()，失败不递归上报。
      const headers = { "Content-Type": "application/json" };
      if (window.__TSI_DESKTOP_TOKEN__) headers["X-Tsi-Desktop-Token"] = window.__TSI_DESKTOP_TOKEN__;
      void fetch("/ui/api/diagnostics", { method: "POST", headers, body: JSON.stringify(payload), keepalive: true }).catch(() => {});
    } catch (_) { /* 离线和窗口关闭不影响请求状态。 */ }
  }
  function errorType(error) { return errors.has(error?.name) ? error.name : "Unknown"; }
  function begin(sessionId, taskId = null, isBusy = () => false) {
    generation += 1;
    busyState = isBusy;
    context = { session_id: sessionId, task_id: taskId, request_id: null, last_event: null };
    started = performance.now();
    terminal = false;
  }
  function observe(event) {
    if (event.request_id) context.request_id = event.request_id;
    if (event.session_id) context.session_id = event.session_id;
    context.last_event = event.type;
    if (terminalTypes.has(event.type)) {
      terminal = true;
      report("terminal_received");
      const current = generation;
      // 即使终态渲染抛错，仍可从独立定时器观察页面是否持续忙碌。
      setTimeout(() => {
        if (generation === current && busyState()) report("terminal_state_stuck");
      }, 1000);
    }
  }
  function settled(isBusy) {
    if (!terminal) return;
    const snapshot = { ...context };
    const current = generation;
    setTimeout(() => {
      // 旧请求的检查不能将新请求的忙碌状态误判为卡住。
      if (generation !== current || context.request_id !== snapshot.request_id) return;
      report(isBusy() ? "terminal_state_stuck" : "terminal_applied");
    }, 250);
  }
  window.addEventListener("error", (event) => {
    const script = (event.filename || "").split("/").pop()?.split(/[?#]/)[0];
    const allowed = ["app.js", "diagnostics.js", "context-settings.js", "model-config.js", "service-config.js"];
    report("script_error", { error_type: errorType(event.error), ...(allowed.includes(script) ? { script, line: event.lineno, column: event.colno } : {}) });
  });
  window.addEventListener("unhandledrejection", (event) => report("unhandled_rejection", { error_type: errorType(event.reason) }));
  return { begin, observe, settled, report, errorType };
})();
