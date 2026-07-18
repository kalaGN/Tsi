# Architecture

## Overview

这是一个基于 Python 3.11 和 FastAPI 的轻量 API 服务。代码按 Application、Router 和 Service 三类职责拆分，根目录 `main.py` 仅保留 Uvicorn 兼容入口。

## Components

- `main.py`：从 `app.application` 导出 FastAPI 应用，保持 `main:app` 启动方式兼容。
- `app/application.py`：创建 FastAPI 应用、注册根路由和 Chat Router。
- `app/routers/chat.py`：声明 `ChatRequest` 并处理 `POST /chat` 的 HTTP 输入输出。
- `app/services/aliyun_responses.py`：管理阿里云上游配置、异步请求和错误映射。
- `tests/test_chat.py`：使用 FastAPI TestClient 和 HTTPX MockTransport 验证接口行为。
- `requirements.txt`：声明运行与测试依赖。
- `.env`：保存本地 API Key，被 Git 忽略。
- `docs/`：保存规格、计划、任务、规则和长期知识。

## Chat Request Flow

```text
Client
  -> POST /chat
  -> app.routers.chat validates ChatRequest
  -> app.services.aliyun_responses reads DASHSCOPE_API_KEY
  -> HTTPX AsyncClient calls Aliyun Responses API (qwen3-max)
  -> Service validates upstream status and JSON
  -> Router returns JSON response
```

## Dependency Direction

```text
main.py
  -> app.application
       -> app.routers.chat
            -> app.services.aliyun_responses
```

- Application 负责组装，不包含上游调用逻辑。
- Router 负责 HTTP 契约，不实现外部协议细节。
- Service 接收普通字符串并返回状态码和 JSON 数据，不依赖 Router 或 Application。

## Design Decisions

- 上游网络 I/O 使用异步 HTTPX 客户端。
- 模型固定为 `qwen3-max`，调用方不能覆盖。
- 成功响应作为不透明 JSON 原样返回，不绑定上游字段结构。
- 上游错误转换为安全、可诊断的 HTTP 错误，不透传敏感信息。
- 当前不继续拆分 Config、Client、Repository 或 Manager 层；只有职责明显增长时再扩展。
