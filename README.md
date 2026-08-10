# FastAPI Demo

这是一个轻量大模型调用项目，同时提供无状态 FastAPI HTTP 接口和可恢复上下文的 Textual TUI，支持阿里云 Responses API、DeepSeek Chat Completions API，以及受限的本地只读工具调用。

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

## 工具调用

HTTP 与 TUI 会把根目录 `tools/` 中显式注册的只读工具提供给模型。模型请求工具时，Runtime 自动执行并把结果回传同一个 Provider，直到模型给出最终文本；用户无需输入工具命令或逐次确认。

第一版只包含：

- `get_current_time(timezone)`：获取指定 IANA 时区（例如 `Asia/Shanghai`）的当前 ISO 8601 时间。

安全和成本边界：

- 工具必须在代码中显式注册；模型不能执行任意 Shell、Python、文件路径、URL 或动态模块。
- 只允许无副作用工具，不支持文件写入、数据库写入、MCP 或动态插件。
- 每个用户请求最多 5 个模型步骤，每步最多 4 个工具调用。
- 单次参数最多 8 KiB、结果最多 32 KiB，均按 UTF-8 字节数计算。
- 多个工具按模型返回顺序串行执行；达到上限时 `/chat` 返回安全的 502。

## 启动 TUI

TUI 会从项目根目录 `.env` 加载配置，Shell 中已设置的环境变量优先。无需先启动 Uvicorn：

```bash
.venv/bin/python -m app.tui
```

TUI 会把已成功的 user/assistant 轮次作为后续请求上下文。Assistant 返回的 Markdown 会在终端中美化为标题、列表、表格和代码块等结构；这只是展示，不会执行代码或改写持久化原文。请求期间，输入框上方会显示动画、`思考中`、实时耗时和 Esc 取消提示；成功或失败后仍会在对话记录中显示最终耗时，取消请求不记录最终耗时。HTTP `/chat` 仍是无状态单轮接口，不与 TUI 共享历史。

- `Enter`：发送输入。
- `↑` / `↓`：向前或向后浏览已发送输入；越过最新记录时恢复浏览前草稿。
- `Esc`：第一次取消运行中请求并提示，1.5 秒内再次按下退出。
- `/clear`：清空界面、模型上下文和本地持久化历史。
- `/quit`：取消运行中请求并退出。

TUI 支持直接使用中文输入法。只有 Assistant 内容按 Markdown 美化，用户输入、系统提示和错误信息仍逐字显示。上下键始终用于输入历史，不承担多行输入的垂直光标移动；粘贴的多行文本仍可原样发送。`/help` 和 `/chat` 不是本地命令，会作为普通文本发送给模型。当前不支持 HTML、远程图片、Mermaid、流式输出、多会话管理、历史搜索、上下文压缩、写操作工具或请求级模型切换。

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

HTTP 和 TUI 每次模型调用都会在 stderr 和本地文件写入同一 request ID 关联的单行 JSON 事件：

```text
logs/model-calls.log
```

不需要工具时，成功调用按固定顺序产生四条可关联事件：

```text
llm_request -> llm_http_request -> llm_http_response -> llm_response
```

- `llm_request`：Runtime 视角的当前输入正文（仅最后一条 user 消息）。
- `llm_http_request`：真实外部 HTTP 边界，记录实际 Provider URL、`POST`、脱敏 Header（`Authorization` 固定写为 `Bearer [REDACTED]`）、完整 JSON 请求体和 `connect_seconds=10 / total_seconds=60` 超时。完整请求体包含 DeepSeek 的 `messages` 或阿里云的 `input`，多轮历史以明文按上游顺序完整保留。
- `llm_http_response`：外部 HTTP 收到响应后立即记录，包含状态码（含非 2xx）、Content-Type 和单调时钟耗时 `duration_ms`，不记录原始响应体。
- `llm_response`：成功统一输出文本。

需要工具时，同一个 request ID 下会出现多组 `llm_http_request/llm_http_response`，并在工具执行处插入：

- `llm_tool_call`：call ID、工具名和参数字符数。
- `llm_tool_result`：call ID、成功/错误状态、耗时和输出字符数。

专用工具事件不重复保存完整参数或结果；实际回传模型的完整工具结果会出现在下一次 `llm_http_request.request_body` 中，因此仍属于下述明文日志风险范围。

连接超时或网络失败时，`llm_http_request` 后写一条 `llm_http_error`，仅包含 `timeout` 或 `connection` 安全分类和耗时，不记录异常类名、异常原文或 Traceback。非 2xx 已收到响应，只写 `llm_http_response`，不再写 `llm_http_error`。

日志不记录环境 API Key、真实 `Authorization`、Provider 原始响应体、Cookie 或异常原文。

> 隐私警告：输入、输出和完整请求体都以明文同时写入 stderr 和本地文件，且多轮历史会在每次调用时重复落盘。不要在提问中粘贴密码、Token、个人隐私或其他不应持久化的数据。具有本地文件读取权限的用户或进程可以读取日志内容。

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
