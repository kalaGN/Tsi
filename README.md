# Tsi 助手

Tsi 助手是一个轻量大模型调用项目，同时提供无状态 FastAPI HTTP 接口和可恢复上下文的 Textual TUI，支持阿里云 Responses API、DeepSeek Chat Completions API，以及带审批和撤销能力的本地项目工具。

## 安装依赖

项目要求 Python 3.11，当前仓库已使用本地 `.venv`：

```bash
cd /Users/wangfei/study/fastapi/demo
.venv/bin/python --version
.venv/bin/python -m pip install -r requirements.txt
```

## 配置模型

配置写入项目根目录 `.env`。该文件已被 Git 忽略，真实密钥不得写入代码、文档或提交记录。

DeepSeek 是默认 Provider；`LLM_PROVIDER` 可以省略：

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=replace-with-real-api-key
DEEPSEEK_MODEL=deepseek-v4-flash
```

使用阿里云时必须显式选择：

```dotenv
LLM_PROVIDER=aliyun
DASHSCOPE_API_KEY=replace-with-real-api-key
ALIYUN_MODEL=qwen3-max
```

`ALIYUN_MODEL`、`DEEPSEEK_MODEL` 都是可选项，空白或未设置时使用示例中的默认模型。Provider 只能为 `aliyun` 或 `deepseek`；显式空白或其他值会返回配置错误。

## 工具调用

HTTP 与 TUI 使用不同的显式工具白名单。HTTP 仅提供只读时间工具；TUI 绑定启动目录，自动执行只读工具，创建、修改或撤销文件时展示完整 Diff 并等待本地确认。Runtime 会把每次结果回传同一个 Provider，直到模型给出最终文本。

| 使用入口 | 工具 | 作用 | 执行方式 |
| --- | --- | --- | --- |
| HTTP、TUI | `get_current_time(timezone)` | 获取指定 IANA 时区（例如 `Asia/Shanghai`）的当前 ISO 8601 时间 | 自动执行 |
| 仅 TUI | `list_workspace_files` | 分页列举允许读取的文件和目录 | 自动执行 |
| 仅 TUI | `search_workspace_text` | 按字面量搜索 UTF-8 文本 | 自动执行 |
| 仅 TUI | `read_workspace_file` | 按行读取文本并返回 SHA-256 | 自动执行 |
| 仅 TUI | `get_workspace_git_status` | 查看 Git 状态 | 自动执行 |
| 仅 TUI | `get_workspace_git_diff` | 查看分页 Diff | 自动执行 |
| 仅 TUI | `apply_workspace_edits` | 创建文件或执行带哈希前置条件的精确替换 | 本地审批后执行 |
| 仅 TUI | `run_project_check` | 运行 `compile`、`test_all`、`pip_check`、`diff_check` 四个固定检查 | 自动执行 |
| 仅 TUI | `undo_workspace_change` | 撤销当前进程最近一次 Agent 修改 | 本地审批后执行 |

典型流程为：模型先列举、搜索、读取和检查现有差异，再提出结构化修改；TUI 显示相对路径和完整有界 Diff，默认焦点为拒绝。确认后模型可运行检查并继续修正。每个新写入和撤销都独立审批；Journal 最多保存 10 个批次且只存在当前 TUI 进程，重启后不能撤销旧批次。

安全和成本边界：

- 工具只能从根目录 `tools/` 显式注册；不提供任意 Shell/Python、动态 import、网络、数据库、依赖安装或 Git 提交/推送。
- Workspace 固定为 TUI 启动目录；绝对路径、`..`、符号链接、二进制和保护路径会被拒绝。
- `.env*`、`.git/`、`.venv/`、`data/`、`logs/` 和缓存目录不可读写；`AGENTS.md`、Rules、依赖文件和 Workspace 安全实现额外禁止写入。
- `apply_workspace_edits` 只支持创建已有目录下的 UTF-8 文件和精确替换，不支持删除、移动、重命名或创建目录。
- HTTP 默认最多 5 个模型步骤、每步 4 次、总计 16 次工具调用；TUI 分别为 20、4、40。
- 普通参数最多 8 KiB，编辑参数最多 64 KiB，结果最多 32 KiB；多个调用串行执行。
- 达到上限时 `/chat` 返回安全的 502；TUI 显示安全错误。已确认并完成的磁盘修改不会因后续模型失败自动回滚，界面会列出仍保留的相对路径。

## 启动 TUI

TUI 会从项目根目录 `.env` 加载配置，Shell 中已设置的环境变量优先。无需先启动 Uvicorn：

```bash
.venv/bin/python -m app.tui
```

TUI 启动时还会读取命令执行目录直属的 `AGENTS.md`：有效的 UTF-8 普通文件会作为每轮请求唯一的首条 system 消息，文件缺失或空白时不启用，正文超过 32 KiB、编码非法或不可读取时会显示错误并阻止模型请求。状态栏以 `AGENTS: loaded|none|error` 展示本次启动结果，不回显正文；文件修改后需重启 TUI 才能生效。当前不递归父目录，不支持 `AGENTS.override.md` 或多文件合并。

TUI 会把已成功的 user/assistant 轮次作为后续请求上下文，系统提示词不会显示在对话区或写入 Session。模型生成期间，输入框上方会持续显示临时纯文本；完整响应到达后，该区域会被一份最终 Markdown 消息替换并美化为标题、列表、表格和代码块等结构。流式展示不会执行代码，也不会把半截回答写入会话历史。请求期间还会显示动画、`思考中`、实时耗时和 Esc 取消提示；成功或失败后仍会在对话记录中显示最终耗时，取消请求不记录最终耗时。HTTP `/chat` 仍是无状态单轮聚合 JSON 接口，不读取 `AGENTS.md`，也不与 TUI 共享历史。

- `Enter`：发送输入。
- `Cmd+A`（macOS）/ `Ctrl+A`：输入框聚焦时全选当前输入内容。
- `↑` / `↓`：向前或向后浏览已发送输入；越过最新记录时恢复浏览前草稿。
- 鼠标拖选消息后按 `Cmd+C`（macOS）或 `Ctrl+C`：复制选中的可见文本。
- 在对话记录中双击某一可见行：立即复制该行的渲染文字；流式输出和审批 Diff 仍使用拖选复制。
- `Esc`：输入框非空时先清空输入；输入为空时第一次取消运行中请求并提示，1.5 秒内再次按下退出。
- `/clear`：清空界面、模型上下文和本地持久化历史。
- `/quit`：取消运行中请求并退出。

TUI 支持直接使用中文输入法。用户输入会用带背景的全宽卡片区分，但仍逐字显示、不解析 Markdown；Assistant 生成中按纯文本增量显示，完成后按 Markdown 美化，系统提示和错误信息保持纯文本。最终消息、流式临时文本和审批 Diff 都可选择复制；对话记录额外支持双击复制单个渲染行。上下键始终用于输入历史，不承担多行输入的垂直光标移动；粘贴的多行文本仍可原样发送。`/help` 和 `/chat` 不是本地命令，会作为普通文本发送给模型。当前不支持 HTML、远程图片、Mermaid、HTTP SSE、多会话管理、历史搜索、上下文压缩、任意命令工具或请求级模型切换。

## 启动 HTTP 服务

```bash
.venv/bin/python -m uvicorn main:app --reload --env-file .env
```

启动后可访问：

- 首页：http://127.0.0.1:8000/
- Swagger：http://127.0.0.1:8000/docs
- ReDoc：http://127.0.0.1:8000/redoc

调用统一模型接口：

```bash
curl --location 'http://127.0.0.1:8000/chat' \
  --header 'Content-Type: application/json' \
  --data '{"input":"你是谁？"}'
```

无论使用哪个 Provider，成功响应都是：

```json
{
  "output_text": "模型生成的文本"
}
```

接口不再返回阿里云或 DeepSeek 的原始响应字段。

## TUI 会话历史

TUI 每个成功轮次都会原子保存到：

```text
data/chat-session.json
```

重新启动 TUI 会自动恢复该文件中的界面消息和模型上下文。如果文件损坏，TUI 不会静默覆盖；输入 `/clear` 可明确删除并重置会话。

输入历史在启动时从已成功保存的 user 消息恢复。本次进程内已经发出但最终失败或取消的输入也可以用上下键找回，但不会写入会话文件；`/clear` 会同时清空会话和输入历史。

工具调用和工具结果只在当前请求内使用，不写入会话文件；一次工具循环完整成功后，只保存用户输入和模型最终回答。

> 隐私警告：会话文件以明文保存输入和回答。`data/` 已被 Git 忽略，但具有本地文件读取权限的用户或进程仍可读取其内容。

## 模型请求日志

HTTP 每次模型调用会把同一 request ID 关联的事件写入 stderr 和本地文件；stderr 保持单行 JSON，本地文件使用适合直接阅读的中文分块格式。TUI 为避免日志覆盖全屏界面，只写本地文件：

```text
logs/model-calls.log
```

本地文件示例：

```text
时间：2026-08-12 14:32:19.311 +08:00
事件：工具结果
请求ID：b7102d8c...
调用ID：call_01
工具：read_workspace_file
状态：成功
耗时：2.73 ms
输出长度：1256 字符

【工具输出】
{
  "ok": true,
  "data": {
    "path": "README.md"
  }
}
================================================================================
```

文件时间统一使用北京时间并包含毫秒和 `+08:00`；请求体、Header、超时配置、工具参数和工具结果会在可解析时缩进为 JSON，模型输入输出保持原始换行。

不需要工具时，成功调用按固定顺序产生四条可关联事件：

```text
llm_request -> llm_http_request -> llm_http_response -> llm_response
```

- `llm_request`：Runtime 视角的当前输入正文（仅最后一条 user 消息）。
- `llm_http_request`：真实外部 HTTP 边界，记录实际 Provider URL、`POST`、脱敏 Header（`Authorization` 固定写为 `Bearer [REDACTED]`）、完整 JSON 请求体和 `connect_seconds=10 / total_seconds=60` 超时。完整请求体包含 DeepSeek 的 `messages` 或阿里云的 `input`，多轮历史以明文按上游顺序完整保留。
- `llm_http_response`：外部 HTTP 收到响应后立即记录，包含状态码（含非 2xx）、Content-Type 和单调时钟耗时 `duration_ms`，不记录原始响应体。
- `llm_response`：成功统一输出文本。

需要工具时，同一个 request ID 下会出现多组 `llm_http_request/llm_http_response`，并在工具执行处插入：

- `llm_tool_call`：call ID、工具名、参数字符数和完整 JSON 参数。
- `llm_tool_result`：call ID、成功/错误状态、耗时、输出字符数和完整工具结果。

工具参数和结果会直接记录在专用工具事件中；实际回传模型的完整工具结果还会出现在下一次 `llm_http_request.request_body` 中。审批事件只记录决定、文件数和 Diff 字符数，不额外记录审批 Diff 正文。

连接超时或网络失败时，`llm_http_request` 后写一条 `llm_http_error`，仅包含 `timeout` 或 `connection` 安全分类和耗时，不记录异常类名、异常原文或 Traceback。非 2xx 已收到响应，只写 `llm_http_response`，不再写 `llm_http_error`。

日志不记录环境 API Key、真实 `Authorization`、Provider 原始响应体、Cookie 或异常原文。

> 隐私警告：输入、输出、工具参数、工具结果、TUI 加载的 `AGENTS.md` 系统提示词和完整请求体都会以明文写入本地文件；HTTP 入口还会同步写入 stderr，且多轮历史会在每次调用时重复落盘。不要在提问、工具参数或 `AGENTS.md` 中放置密码、Token、个人隐私或其他不应发送和持久化的数据。具有本地文件读取权限的用户或进程可以读取日志内容。

单文件转储阈值为 10 MiB，保留 5 个备份；`logs/` 已被 Git 忽略。由于正文不截断，单条超大记录可令当前文件暂时超过该阈值。日志失败不影响模型请求本身。

## 运行测试

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
```

所有外部模型测试均使用 HTTPX MockTransport，不会调用真实接口或消耗额度。

## 提交前检查

```bash
.venv/bin/python -m compileall -q main.py app tools tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
.venv/bin/python -m pip check
git diff --check
```

仓库当前没有 Formatter、Lint、类型检查或 CI，不要把不存在的命令当作现有质量门禁。
