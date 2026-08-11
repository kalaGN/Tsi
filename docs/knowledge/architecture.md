# Architecture

## Overview

这是一个基于 Python 3.11 的轻量模型调用项目，同时提供聚合 JSON 的无状态 FastAPI HTTP 和支持流式展示、可恢复单会话的 Textual TUI。两个入口共享 Chat Runtime；Runtime 通过 Provider Turn 选择阿里云或 DeepSeek，并自动编排根目录白名单只读工具；根目录 `main.py` 只保留 Uvicorn 兼容入口。

## Components

- `main.py`：从 `app.application` 导出 FastAPI 应用。
- `app/application.py`：创建 FastAPI、注册根路由和 Chat Router。
- `app/observability/model_logging.py`：配置模型/HTTP/工具 JSON 日志、stderr 输出、本地转储和事件白名单。
- `app/routers/chat.py`：校验 `POST /chat`，返回统一 `ChatResponse` 并映射 HTTP 错误。
- `app/runtime/chat.py`：无状态入口、有序消息调用、默认工具 Registry、统一结果和安全错误语义。
- `app/runtime/tool_loop.py`：最多 5 个模型步骤、每步最多 4 个工具调用的串行编排与工具事件。
- `app/runtime/session.py`：串行化 TUI 发送，只提交 Provider 和持久化均成功的完整轮次。
- `app/runtime/session_store.py`：版本化 JSON 会话校验、原子保存、恢复与清理。
- `app/services/llm/contracts.py`：中立角色/消息、ModelStep、Provider Turn、文本 Delta/reset 回调协议和共享异常。
- `app/services/llm/factory.py`：解析环境并创建当前 Provider。
- `app/services/llm/http_client.py`：共享异步 SSE POST、有界事件解码、流生命周期超时、I/O 边界事件记录和脱敏错误处理。
- `app/services/llm/aliyun.py`：阿里云 Responses 流事件累加、Function Call 续接和完整结果校验。
- `app/services/llm/deepseek.py`：DeepSeek Chat Completions 流事件累加、assistant/tool 续接和完整结果校验。
- `tools/contracts.py`：Provider 中立的 ToolDefinition、ToolCall、ToolResult 和 Tool 协议。
- `tools/registry.py`：显式白名单注册、参数解析、串行执行、安全错误和载荷边界。
- `tools/builtin.py`：只读 `get_current_time(timezone)` 实现。
- `app/tui/__main__.py`：加载根目录 `.env` 并启动 Textual。
- `app/tui/application.py`：终端输入与历史、用户消息卡片、临时纯文本流、最终 Assistant Markdown、请求活动 Timer、状态、耗时和取消。
- `app/tui/state.py`：定义 `Ready`、`Thinking`、`Error`。
- `tests/test_llm_providers.py`：Provider、工厂和共享 HTTP Mock 测试。
- `tests/test_chat_runtime.py`：Runtime 单元测试。
- `tests/test_tool_loop.py`、`tests/test_tools.py`：有界编排、Registry 和内置工具测试。
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
                                              /          |          \
                                             v           v           v
                              app.observability   tool_loop    provider factory
                                                      |          /          \
                                                      v         v            v
                                                  root tools  AliyunTurn  DeepSeekTurn
                                                                 \          /
                                                                  shared HTTP

python -m app.tui -> configure model logging -> app.tui.application
                                                   -> app.runtime.chat
```

- Router 和 TUI 只依赖 Runtime，不理解外部响应结构。
- Runtime 只依赖 Provider 契约、工厂和根目录工具契约，不导入具体 Provider 模块。
- Tool Loop 只理解 ModelStep、ToolCall、ToolResult 和 Registry，不理解两家上游 JSON。
- 根目录 `tools/` 不依赖 Runtime、Router、TUI 或具体 Provider。
- 工厂只解析配置和创建 Provider，不编排用例。
- Provider 为每个用户请求创建短生命周期 Turn，持有私有续接消息，构造请求并提取中立步骤；共享 HTTP 层处理网络和通用状态错误。
- Provider 层不依赖 Runtime、Router、TUI 或 Application。
- HTTP/TUI 启动入口幂等配置日志；Runtime 记录一次 `llm_request/llm_response`，每个模型步骤记录 HTTP 边界事件，每次本地执行记录 `llm_tool_call/llm_tool_result`，全链路共用同一 request ID。

## HTTP Chat Flow

```text
POST /chat
  -> ChatRequest validates strict nonblank input
  -> Runtime creates the environment-selected Provider and a request ID
  -> Runtime writes llm_request with complete input_text
  -> Runtime creates the default read-only Registry and Provider Turn
  -> bounded loop calls Turn.next()
     -> Provider builds protocol-specific stream payload and calls shared SSE HTTPX
     -> Provider accumulates bounded text/tool events; HTTP supplies no display callback
     -> ModelStep contains final text or ToolCall list
     -> if tools: Registry validates and executes them serially
     -> Runtime passes ordered ToolResult list back to the same Turn
  -> loop stops when Provider returns final output_text
  -> Runtime writes llm_response with complete output_text
  -> Router returns 200 {"output_text": "..."}
```

不需要工具时仍只有四个成功事件。需要工具时，同一 request ID 下会出现多组 HTTP 事件和工具元数据事件。共享 HTTP 层在真实 I/O 边界旁路记录，不修改状态码映射、重试或超时；`llm_http_request.request_body` 与实际 payload 一致，因此续接请求会明文包含工具结果。

DeepSeek Turn 按 choice/tool index 拼接流式文本和工具参数，把 assistant `tool_calls` 和对应 `role=tool/tool_call_id` 结果加入 messages，并保留工具调用时的 `reasoning_content`。阿里云 Turn 消费 `output_text.delta/done`、function call 与 `response.completed`，把每个 `function_call` 与对应 `function_call_output` 紧邻加入 input。两家结构都在 Provider 内转换为中立 ToolCall/ToolResult。

DeepSeek 必须收到合法终止原因和 `[DONE]`；阿里云必须收到成功的 `response.completed`。事件 JSON、UTF-8、终止标记、完成文本或工具结构不一致均属于无效上游响应并映射为 502。单 SSE 事件上限 64 KiB，单步文本上限 1 MiB，单工具参数上限 8 KiB。

## TUI Chat Flow

```text
python -m app.tui
  -> load .env without overriding Shell variables
  -> Runtime resolves Provider, model and safe key status
  -> load data/chat-session.json and restore complete turns
  -> Textual Worker calls ChatSession.send
  -> Runtime sends committed history plus current user message and runs tool loop
  -> Provider text Delta crosses the neutral callback boundary
  -> request-scoped 100 ms Timer batches temporary plain-text output, spinner and elapsed time
  -> tool step reset removes text that is not the final answer
  -> persist the new complete turn atomically
  -> TUI removes temporary output and renders the complete Assistant content as Rich Markdown
  -> TUI stops the Timer, clears activity and records final monotonic elapsed time
```

TUI 不读取 Provider 专属密钥变量，不解析 Provider JSON，也不逐次确认只读工具；状态信息和实际调用共用工厂配置规则。工具轨迹只存在于当前 Turn，工具步骤触发 reset 后其临时文本被清除，Session 仍只提交最终 user/assistant 消息。生成中的文本仅在最高 8 行临时 `RichLog` 中按纯文本展示并由 100 ms Timer 合并刷新；成功后临时区隐藏，完整原文构造为 Rich Markdown Renderable。恢复历史和新响应复用同一最终展示路径；用户消息使用 Rich Panel 增加背景和边框但不解析 Markdown，系统和错误仍按纯文本显示，Session 与后续模型请求继续使用未经改写的原文。请求代次会阻止取消后的陈旧 Delta 写回。HTTP `/chat` 不注册展示回调、不加载或修改 TUI 会话文件，仍在 Runtime 汇总完成后返回 JSON。

输入历史是 `ChatTuiApp` 内存状态：启动时从 Session 的 user 消息初始化，当前进程每次真正启动的请求立即追加，因此失败或取消输入也可临时召回；只有完整成功轮次由既有 Session 规则跨重启保存。高优先级 Up/Down Binding 负责不循环浏览和草稿恢复，不修改 Session schema。

## Configuration

| Provider | Selector | Key | Optional model | Default |
| --- | --- | --- | --- | --- |
| Aliyun | `LLM_PROVIDER=aliyun` | `DASHSCOPE_API_KEY` | `ALIYUN_MODEL` | `qwen3-max` |
| DeepSeek | `LLM_PROVIDER=deepseek` 或未设置 | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL` | `deepseek-v4-flash` |

显式空白或未知 `LLM_PROVIDER` 是配置错误，不静默回退。模型变量空白时使用默认值。上游 URL 固定在相应适配器中，不能通过环境变量覆盖。

## Design Decisions

- HTTP 与 TUI 都只接触统一文本，原始 Provider JSON 只存在于 Provider 调用栈。
- 所有请求统一通过 Provider Turn，不保留旧 `generate()` 或原始 ProviderResult 路径。
- 根目录 Registry 仅注册 `get_current_time(timezone)`；工具名只能来自显式白名单，不支持反射、动态 import、Shell、写操作或 MCP。
- 工具循环最多 5 个模型步骤、每步 4 次调用；参数/结果 UTF-8 上限分别为 8/32 KiB，多个调用串行执行。
- 第 5 步仍请求工具时不执行无法被后续步骤消费的调用，Runtime 返回安全 `tool_limit`，HTTP 映射为 502。
- `/chat` 请求不包含 Provider 或模型；切换由部署环境控制。
- 使用现有异步 HTTPX，不引入 Provider SDK。
- 保持连接 10 秒、从请求开始到流消费结束总计 60 秒超时；不实现自动重试或故障转移。
- SSE 按字节切分边界并严格解码 UTF-8；取消沿调用栈传播并由 HTTPX 上下文关闭响应流。
- 每次调用创建并关闭 HTTP Client；当前没有性能基线，不增加应用级连接生命周期。
- 上游错误体、Authorization、密钥和内部堆栈不进入 HTTP/TUI。
- 模型日志为单行 JSON；Runtime、所有模型步骤和工具事件共用同一 request ID，工具事件另用上游 call ID 关联。
- Runtime 生成 request ID 并显式经 Provider 传给共享 HTTP 层，不使用 ContextVar 或全局当前 ID。
- 输入、输出和完整请求体以明文进入 stderr 和本地文件，多轮历史在每次调用时重复落盘；仍不记录环境 API Key、真实 `Authorization`、Provider 原始响应体、Cookie 或异常原文。
- HTTP 边界脱敏 Header 由日志层用固定值重建（`Authorization` 写为 `Bearer [REDACTED]`），从数据流上阻止密钥进入 Logger。
- HTTP 耗时用 `time.monotonic()` 计算并保留两位毫秒；超时/连接失败只记录有限分类和耗时，不记录异常类名或堆栈。
- 日志双写 stderr 和 UTF-8 `logs/model-calls.log`，单文件 10 MiB，保留 5 个备份；文件不可用时降级为 stderr。
- TUI 同时最多一个请求，第一次 Esc 取消，1.5 秒内第二次 Esc 退出，并用请求代次阻止陈旧结果写回。
- TUI 每个活动请求最多创建一个 100 ms Timer，空闲时没有周期任务；Timer 回调同样校验捕获的请求代次。
- TUI 上下键固定用于输入历史，历史不去重、不循环且没有独立持久化文件；`/clear` 同步清空。
- TUI 使用唯一 `data/chat-session.json` 保存完整轮次；启动恢复，`/clear` 删除，损坏历史不自动覆盖。
- TUI 只对 Assistant 原文做 Rich Markdown 展示，不执行代码、加载远程内容或改变 Session/HTTP 文本契约。
- TUI 临时流只展示当前请求的纯文本；成功、错误、取消、工具 reset 和退出都会清理，部分文本不持久化。
- 会话使用标准库 UTF-8 JSON 和同目录原子替换，不引入数据库或新依赖；历史明文且没有长度裁剪。
- 当前不增加 Repository、Manager、数据库、缓存或其他无实际职责的层级。
