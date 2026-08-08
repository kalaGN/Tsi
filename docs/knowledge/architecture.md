# Architecture

## Overview

这是一个基于 Python 3.11 的轻量模型调用项目，同时提供无状态 FastAPI HTTP 和可恢复单会话的 Textual TUI。两个入口共享 Chat Runtime，Runtime 通过统一 LLM Provider 契约选择阿里云或 DeepSeek；根目录 `main.py` 只保留 Uvicorn 兼容入口。

## Components

- `main.py`：从 `app.application` 导出 FastAPI 应用。
- `app/application.py`：创建 FastAPI、注册根路由和 Chat Router。
- `app/observability/model_logging.py`：配置模型请求 JSON 日志、stderr 输出、本地转储文件，以及请求/响应/失败事件白名单。
- `app/routers/chat.py`：校验 `POST /chat`，返回统一 `ChatResponse` 并映射 HTTP 错误。
- `app/runtime/chat.py`：无状态单轮入口、有序消息调用、统一结果和安全错误语义。
- `app/runtime/session.py`：串行化 TUI 发送，只提交 Provider 和持久化均成功的完整轮次。
- `app/runtime/session_store.py`：版本化 JSON 会话校验、原子保存、恢复与清理。
- `app/services/llm/contracts.py`：中立角色/消息、Provider 协议（含 `request_id`）、内部结果和共享异常。
- `app/services/llm/factory.py`：解析环境并创建当前 Provider。
- `app/services/llm/http_client.py`：共享异步 JSON POST、超时、I/O 边界事件记录和脱敏错误处理。
- `app/services/llm/aliyun.py`：阿里云 Responses 请求和文本提取。
- `app/services/llm/deepseek.py`：DeepSeek Chat Completions 请求和文本提取。
- `app/tui/__main__.py`：加载根目录 `.env` 并启动 Textual。
- `app/tui/application.py`：终端输入、统一文本展示、状态、耗时和取消。
- `app/tui/state.py`：定义 `Ready`、`Thinking`、`Error`。
- `tests/test_llm_providers.py`：Provider、工厂和共享 HTTP Mock 测试。
- `tests/test_chat_runtime.py`：Runtime 单元测试。
- `tests/test_model_logging.py`：日志格式、幂等、转储和失败降级测试。
- `tests/test_chat.py`：HTTP 契约与 Provider 接线测试。
- `tests/test_tui.py`：Textual 无头交互测试。

## Dependency Direction

```text
main.py -> app.application -> app.routers.chat --------+
                    |                                  |
                    +-> configure model logging        |
                                                       v
                                               app.runtime.chat
                                                  /           \
                         llm_request/response JSON             Provider call
                                           |                         |
                                           v                         v
                              app.observability          app.services.llm.factory
                                                                    /          \
                                                                   v            v
                                                   AliyunResponsesProvider  DeepSeekChatProvider
                                                                   \            /
                                                                    +-> shared HTTP

python -m app.tui -> configure model logging -> app.tui.application
                                                   -> app.runtime.chat
```

- Router 和 TUI 只依赖 Runtime，不理解外部响应结构。
- Runtime 只依赖 Provider 契约和工厂，不导入具体 Provider 模块。
- 工厂只解析配置和创建 Provider，不编排用例。
- Provider 构造请求并提取文本；共享 HTTP 层处理网络和通用状态错误。
- Provider 层不依赖 Runtime、Router、TUI 或 Application。
- HTTP/TUI 启动入口幂等配置日志；Runtime 在 Provider 调用前记录 `llm_request`，共享 HTTP 层在真实 I/O 边界记录 `llm_http_request`/`llm_http_response`/`llm_http_error`，Runtime 在成功返回后用同一 request ID 记录 `llm_response`。

## HTTP Chat Flow

```text
POST /chat
  -> ChatRequest validates strict nonblank input
  -> Runtime creates the environment-selected Provider and a request ID
  -> Runtime writes llm_request with complete input_text
  -> Provider builds its protocol-specific payload and calls shared HTTPX
     -> shared HTTPX writes llm_http_request (URL, POST, redacted headers, full payload, timeout)
     -> HTTPX POST
     -> shared HTTPX writes llm_http_response (status, content-type, duration_ms)
  -> Provider validates JSON and extracts output_text
  -> Runtime writes llm_response with complete output_text
  -> Runtime removes raw response details from ChatResult
  -> Router returns 200 {"output_text": "..."}
```

成功路径四个事件共用同一 request ID。共享 HTTP 层在真实 I/O 边界旁路记录，不修改状态码映射、重试或超时；超时或连接失败写 `llm_http_error`（仅 `timeout`/`connection` 分类和耗时），非 2xx 已收到响应只写 `llm_http_response`。`llm_http_request.request_body` 与交给 HTTPX 的实际 payload 一致，DeepSeek 完整保留 `messages`，阿里云完整保留 `input`。

阿里云响应可从顶层 `output_text`、`output[*].text` 或 `output[*].content[*].text` 提取。DeepSeek 固定从 `choices[0].message.content` 提取。无法提取文本属于无效上游响应并映射为 502。

## TUI Chat Flow

```text
python -m app.tui
  -> load .env without overriding Shell variables
  -> Runtime resolves Provider, model and safe key status
  -> load data/chat-session.json and restore complete turns
  -> Textual Worker calls ChatSession.send
  -> Runtime sends committed history plus the current user message
  -> persist the new complete turn atomically
  -> TUI displays ChatResult.output_text directly
  -> TUI records monotonic elapsed time and updates state
```

TUI 不读取 Provider 专属密钥变量，不解析 Provider JSON；状态信息和实际调用共用工厂配置规则。HTTP `/chat` 不加载或修改 TUI 会话文件。

## Configuration

| Provider | Selector | Key | Optional model | Default |
| --- | --- | --- | --- | --- |
| Aliyun | `LLM_PROVIDER=aliyun` | `DASHSCOPE_API_KEY` | `ALIYUN_MODEL` | `qwen3-max` |
| DeepSeek | `LLM_PROVIDER=deepseek` 或未设置 | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL` | `deepseek-v4-flash` |

显式空白或未知 `LLM_PROVIDER` 是配置错误，不静默回退。模型变量空白时使用默认值。上游 URL 固定在相应适配器中，不能通过环境变量覆盖。

## Design Decisions

- HTTP 与 TUI 都只接触统一文本，原始 Provider JSON 只存在于 Provider 调用栈。
- `/chat` 请求不包含 Provider 或模型；切换由部署环境控制。
- 使用现有异步 HTTPX，不引入 Provider SDK。
- 保持连接 10 秒、总计 60 秒超时；不实现自动重试或故障转移。
- 每次调用创建并关闭 HTTP Client；当前没有性能基线，不增加应用级连接生命周期。
- 上游错误体、Authorization、密钥和内部堆栈不进入 HTTP/TUI。
- 模型日志为单行 JSON；`llm_request`、`llm_http_request`、`llm_http_response`、`llm_response` 四个事件共用同一 request ID 关联。
- Runtime 生成 request ID 并显式经 Provider 传给共享 HTTP 层，不使用 ContextVar 或全局当前 ID。
- 输入、输出和完整请求体以明文进入 stderr 和本地文件，多轮历史在每次调用时重复落盘；仍不记录环境 API Key、真实 `Authorization`、Provider 原始响应体、Cookie 或异常原文。
- HTTP 边界脱敏 Header 由日志层用固定值重建（`Authorization` 写为 `Bearer [REDACTED]`），从数据流上阻止密钥进入 Logger。
- HTTP 耗时用 `time.monotonic()` 计算并保留两位毫秒；超时/连接失败只记录有限分类和耗时，不记录异常类名或堆栈。
- 日志双写 stderr 和 UTF-8 `logs/model-calls.log`，单文件 10 MiB，保留 5 个备份；文件不可用时降级为 stderr。
- TUI 同时最多一个请求，第一次 Esc 取消，1.5 秒内第二次 Esc 退出，并用请求代次阻止陈旧结果写回。
- TUI 使用唯一 `data/chat-session.json` 保存完整轮次；启动恢复，`/clear` 删除，损坏历史不自动覆盖。
- 会话使用标准库 UTF-8 JSON 和同目录原子替换，不引入数据库或新依赖；历史明文且没有长度裁剪。
- 当前不增加 Repository、Manager、数据库、缓存或其他无实际职责的层级。
