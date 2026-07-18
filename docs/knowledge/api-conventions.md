# API Conventions

## General

- 请求和响应使用 JSON。
- 请求模型使用 Pydantic，并在服务边界校验输入。
- 路由涉及网络 I/O 时使用 `async def`。
- FastAPI 自动提供 Swagger 文档 `/docs` 和 ReDoc `/redoc`。

## Chat API

```http
POST /chat
Content-Type: application/json
```

```json
{
  "input": "你是谁？"
}
```

- `input` 必须是非空且非纯空白字符串。
- 成功时原样返回上游 JSON。
- 模型固定为 `qwen3-max`。

## Error Mapping

- 请求参数非法：`422 Unprocessable Entity`。
- 未配置上游 API Key：`503 Service Unavailable`。
- 上游超时：`504 Gateway Timeout`。
- 上游连接失败：`502 Bad Gateway`。
- 上游鉴权失败：`401` 或 `403`。
- 上游其他非成功状态：保留状态码，返回安全错误说明。
- 上游成功但响应不是 JSON：`502 Bad Gateway`。

错误响应使用 FastAPI 标准结构：

```json
{
  "detail": "Error description"
}
```

错误内容不得包含 API Key、Authorization 请求头或内部堆栈。
