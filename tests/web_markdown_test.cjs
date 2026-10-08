// 无网络、无新依赖：用最小 DOM 验证真实渲染函数与帧调度。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

class Node {
  constructor(tag, text = "") {
    this.tag = tag; this.text = text; this.childNodes = []; this.style = {};
    this.classList = { add() {}, remove() {} };
  }
  replaceChildren(...nodes) { this.textContent = ""; this.append(...nodes); }
  append(...nodes) {
    for (const value of nodes) {
      const node = typeof value === "string" ? new Node("text", value) : value;
      if (node.tag === "fragment") { this.append(...[...node.childNodes]); continue; }
      node.remove(); node.parent = this; this.childNodes.push(node);
    }
  }
  remove() {
    if (this.parent) this.parent.childNodes.splice(this.parent.childNodes.indexOf(this), 1);
    this.parent = null;
  }
  set textContent(value) { this.childNodes.forEach(node => node.parent = null); this.childNodes = []; this.text = value; }
  get textContent() { return this.text + this.childNodes.map(node => node.textContent).join(""); }
}
const frames = new Map(); let nextFrame = 0;
const sandbox = {
  URL, document: {
    createElement: tag => new Node(tag), createTextNode: text => new Node("text", text),
    createDocumentFragment: () => new Node("fragment"),
  },
  window: {
    requestAnimationFrame: callback => { frames.set(++nextFrame, callback); return nextFrame; },
    cancelAnimationFrame: id => frames.delete(id),
    clearInterval() {}, setInterval() { return 1; },
  },
};
const source = fs.readFileSync(path.join(__dirname, "../app/webui/static/app.js"), "utf8");
vm.createContext(sandbox);
vm.runInContext(source.slice(source.indexOf("function inlineText("), source.indexOf("function addMessage(")), sandbox);
const render = text => sandbox.textBlock(text);
function find(node, tag) { return [...(node.tag === tag ? [node] : []), ...node.childNodes.flatMap(child => find(child, tag))]; }
function frame() { const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(fn => fn()); }

const styles = render("## **标题**\n- *斜体* 和 `代码`\n1. [官网](https://example.com/path)");
assert.equal(find(styles, "strong")[0].textContent, "标题");
assert.equal(find(styles, "em")[0].textContent, "斜体");
assert.equal(find(styles, "code")[0].textContent, "代码");
assert.equal(find(styles, "a")[0].href, "https://example.com/path");
assert.equal(find(styles, "a")[0].rel, "noopener noreferrer");
assert.equal(find(styles, "a")[0].target, "_blank");

const table = render("| 名称 | 内容 |\n| :--- | ---: |\n| **中文** | `a|b` |\n| a\\|b | *值* |");
assert.equal(find(table, "th").length, 2);
assert.equal(find(table, "td").length, 4);
assert.equal(find(table, "th")[0].scope, "col");
assert.equal(find(table, "td")[1].style.textAlign, "right");
assert.equal(find(table, "td")[2].textContent, "a|b");
assert.equal(find(table, "code")[0].textContent, "a|b");
assert.equal(find(render("| a | b |\n| --- |\n| c | d |"), "table").length, 0);
assert.equal(find(render("| a | b |\n| --- | --- |"), "table").length, 1);

const unsafe = '<img src=x onerror=alert(1)> [恶意](javascript:alert) [文件](file:///tmp/x) [相对](/ui)';
const safe = render(unsafe);
assert.equal(find(safe, "img").length, 0);
assert.equal(find(safe, "a").length, 0);
assert.equal(safe.textContent, unsafe);
assert.equal(render("未闭合 **粗体 和 `代码 [链接](http").textContent, "未闭合 **粗体 和 `代码 [链接](http");
const code = render("```js\n**不是粗体**\n\n<img>\n```");
assert.equal(find(code, "strong").length, 0);
assert.equal(find(code, "code")[0].textContent, "**不是粗体**\n\n<img>");

let paints = 0;
const target = new Node("div");
const stream = sandbox.createMarkdownStream(target, () => paints++);
stream.append("**中"); stream.append("文");
assert.equal(frames.size, 1); frame();
assert.equal(target.textContent, "**中文");
stream.append("**\n\n"); frame();
const committed = find(target, "strong")[0];
stream.append("| A | B |\n| --- | --- |\n| 1 | "); frame();
assert.equal(find(target, "table").length, 1);
stream.append("**2** |\n\n"); frame();
assert.equal(find(target, "strong")[0], committed); // 已完成块保留 DOM 身份。
assert.equal(find(target, "td")[1].textContent, "2");
stream.append("```\n第一行\n\n第二行"); frame();
assert.equal(find(target, "pre").length, 1);
stream.append("\n```\n\n结束"); frame();
assert.equal(find(target, "pre").length, 1);
stream.append("陈旧内容");
stream.dispose();
assert.equal(frames.size, 0);
frame();
assert(!target.textContent.includes("陈旧内容"));
assert.equal(paints, 6);

// 使用真实进度/终态函数，确认工具阶段及最终回答不会被迟到的帧覆盖。
sandbox.state = { streamNode: new Node("div"), streamRenderer: null };
sandbox.scrollToBottom = () => {};
vm.runInContext(source.slice(source.indexOf("function taskFeedbackSummary("), source.indexOf("function showToolApproval(")), sandbox);
vm.runInContext(source.slice(source.indexOf("function finishStreamAsMarkdown("), source.indexOf("async function consumeEventStream(")), sandbox);
sandbox.startTaskFeedback("测试任务");
sandbox.beginModelOutput();
sandbox.state.streamRenderer.append("**旧输出**");
sandbox.resumeTaskFeedback("工具执行中");
assert.equal(frames.size, 0);
assert(sandbox.state.streamNode.textContent.includes("工具执行中"));
sandbox.beginModelOutput();
sandbox.state.streamRenderer.append("**工具后的新输出**"); frame();
assert.equal(find(sandbox.state.streamNode, "strong")[0].textContent, "工具后的新输出");
const finalNode = sandbox.state.streamNode;
sandbox.state.streamRenderer.append("迟到增量");
sandbox.finishStreamAsMarkdown("**最终回答**");
assert.equal(frames.size, 0); frame();
assert.equal(finalNode.textContent, "最终回答");
assert.equal(sandbox.state.streamNode, null);
assert.equal(sandbox.state.streamRenderer, null);
console.log("Markdown 安全、表格、增量缓存与帧调度测试通过");
