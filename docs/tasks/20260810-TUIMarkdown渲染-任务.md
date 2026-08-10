# TUI Markdown 渲染任务

状态：已完成（2026-08-10）

对应[需求](../spec/20260810-TUIMarkdown渲染-需求.md)和[设计](../plan/20260810-TUIMarkdown渲染-设计.md)。

## Task 1：锁定 Markdown 与纯文本展示边界

- [x] 为新 Assistant Markdown 增加 headless TUI 回归测试。
- [x] 为恢复历史中的 Assistant Markdown 增加回归测试。
- [x] 验证用户 Markdown 标记仍逐字显示。

验收：新增测试在实现前能证明当前纯文本行为不满足需求，实现后全部通过。

验证：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_tui.py
```

可能修改：`tests/test_tui.py`。

依赖：无。

## Task 2：实现 Assistant Markdown Renderable

- [x] Assistant 标签与正文独立写入 `RichLog`。
- [x] Assistant 正文使用 `rich.markdown.Markdown`。
- [x] `You`、`System`、`Error` 保持纯文本路径。

验收：标题、列表、中文和 fenced code block 被美化；原始 Session/HTTP 契约不变。

验证：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_tui.py
```

可能修改：`app/tui/application.py`、`tests/test_tui.py`。

依赖：Task 1。

## Checkpoint：核心展示

- [x] TUI 专项测试通过。
- [x] 普通文本、恢复历史、错误、系统消息和快捷键测试无回归。

## Task 3：同步使用说明与架构事实

- [x] README 说明 Assistant Markdown 展示能力与非执行边界。
- [x] Knowledge 说明展示层职责和原文持久化边界。
- [x] 更新 Spec/Plan/Tasks 状态和目录索引。

验收：文档不声称支持 HTML、图片、Mermaid 或代码执行，链接有效且事实与代码一致。

验证：

```bash
git diff --check
```

可能修改：`README.md`、`docs/knowledge/architecture.md`、`docs/knowledge/project-overview.md`、SDD 文档及索引。

依赖：Task 2。

## Task 4：全量验证与审查

- [x] 运行语法检查、全量测试、依赖检查和差异检查。
- [x] 审查正确性、回归、安全、性能和维护性。
- [x] 确认没有真实模型请求、依赖变更或 HTTP 契约变化。

验收：全部项目真实门禁通过且无阻塞审查发现。

验证：

```bash
.venv/bin/python -m compileall -q main.py app tools tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
.venv/bin/python -m pip check
git diff --check
```

可能修改：仅限修复审查发现涉及的本次文件。

依赖：Task 3。
