# API Conventions

## General

- 请求和响应使用 JSON。
- 请求模型使用 Pydantic，并在服务边界校验输入。
- 路由涉及网络 I/O 时使用 `async def`。
- FastAPI 自动提供 Swagger 文档 `/docs` 和 ReDoc `/redoc`。
- 对话 API 已移除；当前唯一的 HTTP 入口是本机 Web UI，全部接口只接受 loopback 客户端。

## Web API

页面挂载在 `/ui`，数据接口挂载在 `/ui/api/`：

- `GET /ui/api/bootstrap`：返回当前会话、模型和启动健康状态。
- `POST /ui/api/chat`：对话入口，以 `application/x-ndjson` 流式返回有序事件。
- `POST /ui/api/cancel`、`POST /ui/api/tool-approvals/{approval_id}`：取消当前请求，或提交一次性审批决定。
- `/ui/api/sessions`、`/ui/api/model`、`/ui/api/files`：多会话管理、模型切换和工作区读取。

Provider 由部署环境中的 `LLM_PROVIDER` 选择，调用方不能在对话请求中覆盖；模型切换走独立的 `/ui/api/model` 接口，并复用与 TUI 相同的模型选择文件。

## Error Mapping

- 非 loopback 客户端：`403 Forbidden`。
- 未配置上游 API Key，或会话存储与 Runtime 不可用：`503 Service Unavailable`。
- 已有请求正在运行，或审批决定与当前请求不匹配：`409 Conflict`。
- 请求参数、审批参数或会话参数非法：`422 Unprocessable Entity`。
- 文件不可读取、审批已失效或会话不存在：`404 Not Found`。

上游失败（超时、连接、鉴权、非法响应）不映射为 HTTP 状态码，而是作为 `/ui/api/chat` 流中的 `failed` 事件返回。

错误响应使用 FastAPI 标准结构：

```json
{
  "detail": "Error description"
}
```

错误内容不得包含 API Key、Authorization 请求头或内部堆栈。
