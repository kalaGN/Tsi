# Architecture

## Overview

这是一个基于 Python 3.11 的轻量单轮模型调用项目，同时提供 FastAPI HTTP 和 Textual TUI。两个入口共享 Chat Runtime，Runtime 通过统一 LLM Provider 契约选择阿里云或 DeepSeek；根目录 `main.py` 只保留 Uvicorn 兼容入口。

## Components

- `main.py`：从 `app.application` 导出 FastAPI 应用。
- `app/application.py`：创建 FastAPI、注册根路由和 Chat Router。
- `app/routers/chat.py`：校验 `POST /chat`，返回统一 `ChatResponse` 并映射 HTTP 错误。
- `app/runtime/chat.py`：共享单轮用例、统一结果、配置状态和安全错误语义。
- `app/services/llm/contracts.py`：Provider 协议、内部结果和共享异常。
- `app/services/llm/factory.py`：解析环境并创建当前 Provider。
- `app/services/llm/http_client.py`：共享异步 JSON POST、超时和脱敏错误处理。
- `app/services/llm/aliyun.py`：阿里云 Responses 请求和文本提取。
- `app/services/llm/deepseek.py`：DeepSeek Chat Completions 请求和文本提取。
- `app/tui/__main__.py`：加载根目录 `.env` 并启动 Textual。
- `app/tui/application.py`：终端输入、统一文本展示、状态、耗时和取消。
- `app/tui/state.py`：定义 `Ready`、`Thinking`、`Error`。
- `tests/test_llm_providers.py`：Provider、工厂和共享 HTTP Mock 测试。
- `tests/test_chat_runtime.py`：Runtime 单元测试。
- `tests/test_chat.py`：HTTP 契约与 Provider 接线测试。
- `tests/test_tui.py`：Textual 无头交互测试。

## Dependency Direction

```text
main.py -> app.application -> app.routers.chat --------+
                                                       v
                                               app.runtime.chat
                                                       v
                                            app.services.llm.factory
                                             /                    \
                                            v                      v
                              AliyunResponsesProvider    DeepSeekChatProvider
                                            \                      /
                                             +--> shared HTTP ----+

python -m app.tui -> app.tui.application -> app.runtime.chat
```

- Router 和 TUI 只依赖 Runtime，不理解外部响应结构。
- Runtime 只依赖 Provider 契约和工厂，不导入具体 Provider 模块。
- 工厂只解析配置和创建 Provider，不编排用例。
- Provider 构造请求并提取文本；共享 HTTP 层处理网络和通用状态错误。
- Provider 层不依赖 Runtime、Router、TUI 或 Application。

## HTTP Chat Flow

```text
POST /chat
  -> ChatRequest validates strict nonblank input
  -> Runtime creates the environment-selected Provider
  -> Provider sends its protocol-specific request through shared HTTPX
  -> Provider validates JSON and extracts output_text
  -> Runtime removes raw response details from ChatResult
  -> Router returns 200 {"output_text": "..."}
```

阿里云响应可从顶层 `output_text`、`output[*].text` 或 `output[*].content[*].text` 提取。DeepSeek 固定从 `choices[0].message.content` 提取。无法提取文本属于无效上游响应并映射为 502。

## TUI Chat Flow

```text
python -m app.tui
  -> load .env without overriding Shell variables
  -> Runtime resolves Provider, model and safe key status
  -> Textual Worker calls the same run_chat use case
  -> TUI displays ChatResult.output_text directly
  -> TUI records monotonic elapsed time and updates state
```

TUI 不读取 Provider 专属密钥变量，不解析 JSON；状态信息和实际调用共用工厂配置规则。

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
- TUI 同时最多一个请求，第一次 Esc 取消，1.5 秒内第二次 Esc 退出，并用请求代次阻止陈旧结果写回。
- 当前不增加 Repository、Manager、数据库、缓存或其他无实际职责的层级。
