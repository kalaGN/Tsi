// 使用隔离 VM 模拟浏览器，无网络、真实模型或业务数据。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const root = path.join(__dirname, "../app/webui/static");
const reports = [];
const timers = [];
const listeners = {};
const sandbox = {
  window: { addEventListener: (name, fn) => listeners[name] = fn, __TSI_DESKTOP_TOKEN__: "private-token" },
  fetch: (_, options) => { reports.push({ payload: JSON.parse(options.body), headers: options.headers }); return Promise.resolve({}); },
  performance: { now: () => 100 }, Date,
  setTimeout: fn => timers.push(fn), Uint8Array, TextDecoder,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(root, "diagnostics.js"), "utf8"), sandbox);
const diagnostics = sandbox.window.TsiDiagnostics;
sandbox.TsiDiagnostics = diagnostics;
diagnostics.begin("a".repeat(32), null, () => true);
diagnostics.observe({ type: "completed", request_id: "b".repeat(32) });
timers.shift()();
assert.equal(reports.at(-1).payload.event, "terminal_state_stuck");
assert.equal(reports.at(-1).headers["X-Tsi-Desktop-Token"], "private-token");
listeners.unhandledrejection({ reason: { name: "TypeError", message: "secret", stack: "private" } });
assert.equal(reports.at(-1).payload.error_type, "TypeError");
assert(!JSON.stringify(reports.map(item => item.payload)).includes("private-token"));
assert(!JSON.stringify(reports.map(item => item.payload)).includes("secret"));
diagnostics.observe({ type: "completed", request_id: "b".repeat(32) });
diagnostics.begin("a".repeat(32));
const count = reports.length;
timers.shift()();
assert.equal(reports.length, count); // 旧请求的检查不影响新请求。
const app = fs.readFileSync(path.join(root, "app.js"), "utf8");
const helper = app.slice(app.indexOf("async function consumeEventStream("), app.indexOf("async function sendMessage("));
let terminal = false;
sandbox.handleEvent = event => { diagnostics.observe(event); terminal = event.type === "completed"; return terminal; };
vm.runInContext(helper, sandbox);
function response(lines) {
  const chunks = [...lines.map(line => new TextEncoder().encode(line))];
  return { body: { getReader: () => ({ read: async () => chunks.length ? { value: chunks.shift(), done: false } : { done: true }, cancel: async () => {}, releaseLock: () => {} }) } };
}
(async () => {
  diagnostics.begin("a".repeat(32));
  await sandbox.consumeEventStream(response(['{"type":"request_started","request_id":"cccc"}\n', '{"type":"completed"}\n']));
  assert(terminal);
  await assert.rejects(sandbox.consumeEventStream(response(['{"type":"text_delta","text":"hi"}\n'])), /连接已中断/);
  assert.equal(reports.at(-1).payload.event, "stream_disconnected");
  await assert.rejects(sandbox.consumeEventStream(response(['secret-not-json\n'])), /解析失败/);
  assert.equal(reports.at(-1).payload.event, "stream_parse_error");
  assert(!JSON.stringify(reports.map(item => item.payload)).includes("secret-not-json"));
})().catch(error => { console.error(error); process.exitCode = 1; });
