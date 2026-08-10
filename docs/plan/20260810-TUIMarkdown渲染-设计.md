# TUI Markdown 渲染设计

状态：已完成（2026-08-10）

对应[需求](../spec/20260810-TUIMarkdown渲染-需求.md)和[任务](../tasks/20260810-TUIMarkdown渲染-任务.md)。

## 1. 当前事实

- `ChatTuiApp` 使用一个 `RichLog` 展示恢复历史、新消息、错误和系统提示。
- `_write_message()` 当前把粗体角色标签和正文拼成同一个 `rich.text.Text`，因此 Markdown 标记按原文显示。
- 启动恢复和新响应都调用 `_write_message("Assistant", content)`，已有统一入口。
- `textual==8.2.8` 已传递依赖 Rich，当前环境可直接导入 `rich.markdown.Markdown`。
- Session 保存 Provider 中立的原始 `ChatMessage.content`；展示层没有回写 Session。

## 2. 方案

保留 `_write_message(role, content)` 作为所有角色的统一调用入口，在其中按明确角色分派：

```python
def _write_message(self, role: str, content: str) -> None:
    if role == "Assistant":
        self._write_assistant_message(content)
        return
    self._write_plain_message(role, content)
```

Assistant 路径向同一个 `RichLog` 连续写入：

1. 独立的粗体 `Assistant` 标签 `Text`。
2. `Markdown(content)` Renderable。

其他角色继续构造单个 `Text`，保留当前逐字显示语义。

## 3. 关键取舍

### 3.1 角色标签不进入 Markdown

不使用 `Markdown(f"Assistant\n{content}")`，避免模型正文通过前导语法改变角色标签的结构，也避免标签与首段被解析成同一段落。

### 3.2 展示与数据分离

只在 `app/tui/application.py` 构造 Rich Renderable：

```text
Provider output_text ──> ChatResult / Session 原文
                              │
                              └──> TUI Markdown Renderable
```

不修改 `ChatResult`、`ChatMessage`、Provider、Runtime、Session Store 或 HTTP Router。

### 3.3 纯文本安全边界

- `You`、`System`、`Error` 不进入 Markdown 解析器。
- Rich Markdown 只构造展示对象，不执行 fenced code、Shell、HTML、动态 import 或工具。
- 不打开链接、不加载远程图片、不新增网络请求。

## 4. 测试设计

在现有 `tests/test_tui.py` 增加 headless 行为测试：

1. 新 Assistant 回答包含标题、中文列表和 Python 代码块时，渲染后的 transcript 保留正文内容但不显示 Markdown 标记。
2. 恢复历史中的 Assistant Markdown 使用同一路径美化。
3. 用户输入中的 `#`、`**` 等标记仍逐字显示。
4. 现有普通回答、错误、系统、历史、清空、快捷键和取消测试作为回归证据。

测试不依赖具体颜色或终端主题，只断言稳定文本结构，避免绑定 Rich 的 ANSI 样式细节。

## 5. 文档同步

- README：TUI 能力与限制中说明 Assistant Markdown 美化及“只展示、不执行”。
- `docs/knowledge/project-overview.md`：补充当前 TUI 输出能力。
- `docs/knowledge/architecture.md`：明确 Markdown 只属于展示层，不改变 Runtime/Session 原文。
- Spec/Plan/Tasks 和三个索引更新最终状态。

## 6. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| Markdown Renderable 改变 transcript 分行 | 旧测试或展示断言失败 | 使用内容级断言，并保留角色标签顺序 |
| 角色标签被正文语法影响 | 角色不清晰 | 标签与正文分为两个 Renderable |
| 用户/错误内容被解释 | 回显失真 | 只按明确的 Assistant 角色分派 |
| 宽表格或代码块在窄终端拥挤 | 可读性下降 | 继续使用 RichLog wrap，第一版不增加横向滚动系统 |

## 7. 回滚

回滚仅需恢复 `app/tui/application.py` 的纯文本 `_write_message()` 和对应测试/文档；没有数据迁移、依赖卸载、Session schema 或 HTTP 兼容问题。

## 8. 验证

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_tui.py
.venv/bin/python -m compileall -q main.py app tools tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
.venv/bin/python -m pip check
git diff --check
```
