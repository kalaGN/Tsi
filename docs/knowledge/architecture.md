# Architecture

## Overview

这是一个基于 Python 3.11 的轻量单轮模型调用项目，同时提供 FastAPI HTTP 和 Textual TUI 两个入口。两个界面共享 Chat Runtime，根目录 `main.py` 仅保留 Uvicorn 兼容入口。

## Components

- `main.py`：从 `app.application` 导出 FastAPI 应用，保持 `main:app` 启动方式兼容。
- `app/application.py`：创建 FastAPI 应用、注册根路由和 Chat Router。
- `app/routers/chat.py`：声明 `ChatRequest`，处理 `POST /chat` 的 HTTP 输入输出和 Runtime 错误映射。
- `app/runtime/chat.py`：提供界面无关的单轮 Chat 用例、结果和安全错误语义。
- `app/services/aliyun_responses.py`：管理阿里云上游配置、异步请求、响应解析和 Provider 异常。
- `app/tui/__main__.py`：加载项目根目录 `.env` 并启动 Textual 应用。
- `app/tui/application.py`：处理终端输入、记录展示、运行状态、命令和请求取消。
- `app/tui/state.py`：定义 `Ready`、`Thinking`、`Error` 状态。
- `tests/test_chat.py`：使用 FastAPI TestClient 和 HTTPX MockTransport 验证接口行为。
- `tests/test_chat_runtime.py`：验证共享 Runtime 的结果和错误转换。
- `tests/test_tui.py`：使用 Textual 无头测试验证 TUI 交互，不访问真实网络。
- `requirements.txt`：声明运行与测试依赖。
- `.env`：保存本地 API Key，被 Git 忽略。
- `docs/`：保存规格、计划、任务、规则和长期知识。

## HTTP Chat Request Flow

```text
Client
  -> POST /chat
  -> app.routers.chat validates ChatRequest
  -> app.runtime.chat executes the single-turn use case
  -> app.services.aliyun_responses reads DASHSCOPE_API_KEY
  -> HTTPX AsyncClient calls Aliyun Responses API (qwen3-max)
  -> Service validates upstream status and JSON
  -> Runtime returns ChatResult or a UI-neutral ChatRuntimeError
  -> Router returns the original success JSON or maps the error to HTTP detail
```

## TUI Chat Request Flow

```text
python3 -m app.tui
  -> app.tui.__main__ loads .env without overriding Shell variables
  -> ChatTuiApp validates input and starts one async Textual Worker
  -> app.runtime.chat executes the single-turn use case
  -> app.services.aliyun_responses calls Aliyun Responses API
  -> TUI displays extracted response text or formatted JSON
  -> Worker completion changes Thinking to Ready or Error
```

## Dependency Direction

```text
main.py -> app.application -> app.routers.chat --+
                                                |
                                                v
                                      app.runtime.chat
                                                |
                                                v
                              app.services.aliyun_responses
                                                ^
                                                |
python3 -m app.tui -> app.tui.application ------+
```

- Application 负责组装，不包含上游调用逻辑。
- Router 负责 HTTP 契约和 HTTP 错误映射，不实现外部协议细节。
- TUI 负责终端交互和状态，不导入 Router、FastAPI Application 或 `main.py`。
- Runtime 负责共享单轮用例，不依赖 FastAPI 或 Textual。
- Service 接收普通字符串并返回状态码和 JSON 数据，不依赖 Runtime、Router、TUI 或 Application。

## Design Decisions

- 上游网络 I/O 使用异步 HTTPX 客户端。
- 模型固定为 `qwen3-max`，调用方不能覆盖。
- 成功响应作为不透明 JSON 原样返回，不绑定上游字段结构。
- TUI 展示时优先提取已知文本字段，无法提取时降级为格式化 JSON；这不改变 HTTP 契约。
- 上游错误先转换为安全 Runtime 错误，再由 HTTP/TUI 边界分别呈现，不透传敏感信息。
- TUI 使用 Textual async Worker，单实例同时最多一个请求；Ctrl+C 可取消且用请求代次阻止陈旧结果写入。
- 当前不继续拆分 Config、Client、Repository 或 Manager 层；只有职责明显增长时再扩展。
