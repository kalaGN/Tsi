# TUI Markdown 渲染需求

状态：已完成（2026-08-10）

## 1. 背景与目标

模型接口返回的 `output_text` 经常包含 Markdown 标题、列表、引用、强调、链接、表格和代码块。
当前 TUI 将 Assistant 内容作为普通 `Text` 写入 `RichLog`，Markdown 标记会原样显示，终端阅读体验较差。

本次目标是在不改变 Provider、Runtime、Session 和 HTTP `/chat` 契约的前提下，只对 TUI 中的
Assistant 内容进行 Rich Markdown 渲染，让结构化回答在终端中更易读。

## 2. 默认假设

1. 只有 `Assistant` 内容按 Markdown 渲染；`You`、`System` 和 `Error` 内容继续按纯文本显示。
2. TUI 启动时恢复的历史 Assistant 消息与新收到的 Assistant 消息使用相同渲染路径。
3. Provider 返回空文本的校验仍由既有边界负责，本次不改变错误映射。
4. 使用项目已有 Rich/Textual 能力，不新增 Markdown 或语法高亮依赖。
5. Markdown 只影响终端展示；Session 仍持久化原始 `output_text`，后续模型上下文也继续使用原文。

## 3. 展示行为

- Assistant 标签仍清晰可见，并与 Markdown 正文分隔。
- 支持 Rich Markdown 已提供的标题、段落、列表、引用、强调、链接、表格和 fenced code block。
- 代码块保留缩进和换行，并在终端宽度变化时保持可读。
- 原始 Markdown 不在写入 Session 前被替换、清洗或转换为 ANSI 文本。
- 用户输入中的 Markdown 标记不渲染，避免回显内容与用户实际输入不一致。
- 错误与系统消息不渲染，避免上游错误文案或控制提示被解释成展示结构。

示例：

````markdown
## 结果

- 第一项
- 第二项

```python
print("hello")
```
````

终端中应呈现为格式化标题、列表和代码块，而不是原样显示 `##`、`-` 与代码围栏。

## 4. 项目结构与实现边界

预计只修改：

```text
app/tui/application.py   # 区分纯文本消息与 Assistant Markdown Renderable
tests/test_tui.py        # 新消息、恢复消息、纯文本边界和复杂 Markdown 回归
README.md                # 说明 TUI Markdown 展示能力
docs/knowledge/          # 同步已落地的 TUI 事实
```

- 不把 Rich、Textual 或 Markdown 类型引入 `app/runtime/`、`app/services/llm/` 或根目录 `tools/`。
- 不修改 `ChatResult`、`ChatMessage` 或 Session JSON schema。
- 不修改 HTTP `/chat` 的 `{"output_text": "..."}` 成功响应。

## 5. 代码风格与安全

- 使用 Rich 提供的 Renderable，不拼接终端转义序列。
- 角色标题与内容渲染职责保持清晰，不根据正文猜测角色。
- 只为 Markdown/纯文本安全边界增加必要注释，不逐行复述实现。
- Markdown 渲染不得执行代码块、Shell、HTML 脚本、动态 import 或工具调用。
- 原始模型文本仍遵循现有本地日志和明文 Session 边界，本次不扩大记录范围。

## 6. 测试策略与命令

- Headless TUI 测试验证 Assistant Markdown 被作为 Markdown Renderable 写入。
- 覆盖标题/列表/代码块、中文 Markdown 和普通纯文本。
- 验证用户、错误、系统消息仍按纯文本处理。
- 验证恢复的历史 Assistant 内容也走 Markdown 渲染。
- 自动化测试使用假 Runner，不调用真实模型。

```bash
.venv/bin/python -m compileall -q main.py app tools tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
.venv/bin/python -m pip check
git diff --check
```

## 7. 非目标

- 不支持终端内编辑或复制 Markdown 源码的专用模式。
- 不增加主题配置、代码块复制按钮、图片下载、HTML 渲染或 Mermaid 渲染。
- 不根据 Markdown 内容自动调用链接、命令或工具。
- 不改变模型 Prompt 来强制其输出 Markdown。
- 不改变 TUI 输入框、状态栏、快捷键或耗时展示。

## 8. 可测试验收标准

1. 新收到的 Assistant Markdown 在 TUI 中按 Rich Markdown 美化显示。
2. TUI 重启恢复历史时，Assistant Markdown 使用相同的美化效果。
3. 用户输入、系统提示和错误文案继续逐字显示，不解释 Markdown 标记。
4. Markdown 渲染不修改持久化原文，也不改变下一轮发送给 Provider 的上下文。
5. 代码块仅展示，不执行其中内容；不增加第三方依赖。
6. HTTP `/chat`、中文输入、回车发送、双 Esc 退出、`/clear`、耗时和上下文持久化行为不回归。
7. 全部真实质量门禁通过。

## 9. 待确认决策

1. 只美化 Assistant 输出，其他角色保持纯文本。
2. 恢复历史和新响应都使用 Rich Markdown。
3. 保留原始 Markdown 作为 Session 和模型上下文，渲染只发生在 TUI 展示层。
4. 不增加依赖，也不支持 HTML、图片、Mermaid 或代码执行。
