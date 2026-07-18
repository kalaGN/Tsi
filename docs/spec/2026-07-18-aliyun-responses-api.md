# Spec: 阿里云 Responses API 对接

## Objective

在当前 FastAPI Demo 中新增 `POST /chat` 接口，由服务端调用阿里云兼容模式的 Responses API，并将上游 JSON 响应原样返回给调用方。

目标用户是需要通过本地 FastAPI 服务访问 `qwen3-max` 模型的 API 调用方。服务端负责隐藏上游地址和鉴权信息，并将超时、鉴权失败及上游异常转换为清晰的 HTTP 错误响应。

本期范围：

- 接收单轮文本输入。
- 固定调用 `qwen3-max` 模型。
- 原样透传上游成功响应。
- 使用环境变量管理 API Key。
- 以异步方式调用上游接口。

本期不包含：

- 流式输出。
- 多轮对话历史。
- 调用方自定义模型。
- 工具调用。
- 图片或其他多模态输入。

## API Contract

### Request

```http
POST /chat
Content-Type: application/json
```

```json
{
  "input": "你是谁？"
}
```

约束：

- `input` 必须是非空字符串。
- 缺少字段、类型错误或空字符串时返回 `422 Unprocessable Entity`。

### Success Response

- 状态码：使用上游成功响应的状态码，正常情况为 `200 OK`。
- 响应体：原样返回阿里云 Responses API 的 JSON 响应。
- 响应类型：`application/json`。

### Error Response

服务端使用统一 JSON 结构返回可诊断错误，但不泄露 API Key：

```json
{
  "detail": "Upstream request timed out"
}
```

错误映射：

- 未配置 `DASHSCOPE_API_KEY`：`503 Service Unavailable`。
- 上游鉴权失败：透传上游 `401` 或 `403` 状态码，并返回安全的错误说明。
- 上游请求超时：`504 Gateway Timeout`。
- 上游连接失败：`502 Bad Gateway`。
- 上游其他非成功响应：透传上游状态码；响应内容须经过安全处理，不返回敏感请求信息。
- 上游返回非 JSON 内容：`502 Bad Gateway`。

## Upstream Integration

请求地址：

```text
https://llm-h2k07hgnp4aylibi.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/responses
```

请求头：

```text
Authorization: Bearer ${DASHSCOPE_API_KEY}
Content-Type: application/json
Accept: application/json
```

不手动设置 `Host`、`Connection` 或 Apifox 的 `User-Agent`，由 HTTP 客户端正确管理这些协议级请求头。

请求体：

```json
{
  "model": "qwen3-max",
  "input": "你是谁？"
}
```

配置项：

- `DASHSCOPE_API_KEY`：必填，上游 Bearer Token。
- 上游 URL、模型名和超时先作为代码常量集中管理；如后续需要多环境配置，再扩展为环境变量。

## Tech Stack

- Python 3.11+
- FastAPI 0.125.x
- Pydantic 1.10.x
- Uvicorn 0.49.x
- HTTPX：异步上游 HTTP 调用
- Pytest：自动化测试
- FastAPI TestClient 或 HTTPX ASGITransport：接口测试

HTTPX 和测试依赖若当前环境或项目依赖清单中不存在，须在实现前明确添加并记录到依赖文件。

## Commands

开发启动：

```bash
python3 -m uvicorn main:app --reload
```

设置本地 API Key：

```bash
export DASHSCOPE_API_KEY='replace-with-real-api-key'
```

执行测试：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
```

语法检查：

```bash
python3 -m compileall -q main.py tests
```

手工调用本地接口：

```bash
curl --location 'http://127.0.0.1:8000/chat' \
  --header 'Content-Type: application/json' \
  --data '{"input":"你是谁？"}'
```

## Project Structure

```text
demo/
├── main.py                              # FastAPI 应用、请求模型与路由
├── requirements.txt                    # 运行及测试依赖
├── README.md                            # 启动和使用说明
├── docs/
│   └── spec/
│       └── 2026-07-18-aliyun-responses-api.md # 本规格
└── tests/
    └── test_chat.py                     # /chat 接口测试
```

当前项目规模较小，本期不为单个上游调用提前拆分多层目录。若 `main.py` 后续继续增长，再将上游客户端和配置拆分到独立模块。

## Code Style

- 使用 Python 类型标注。
- 请求和配置数据使用语义明确的名称。
- 网络 I/O 使用 `async def` 和异步 HTTP 客户端。
- 不使用宽泛的裸 `except`；分别处理超时、连接错误和 HTTP 状态错误。
- 不在日志或异常响应中输出 Authorization 请求头。

示例风格：

```python
class ChatRequest(BaseModel):
    input: str = Field(min_length=1)


@app.post("/chat")
async def create_chat(request: ChatRequest):
    return await request_upstream_response(request.input)
```

## Testing Strategy

测试不得请求真实阿里云接口，也不得消耗模型额度。通过模拟 HTTPX 上游响应覆盖以下场景：

1. 有效输入时，上游请求使用固定模型、正确输入及 Bearer Token。
2. 上游成功时，`/chat` 原样返回 JSON 和成功状态码。
3. 空字符串、缺少 `input`、错误字段类型返回 `422`。
4. 缺少 `DASHSCOPE_API_KEY` 返回 `503`，且不会发起上游请求。
5. 上游超时返回 `504`。
6. 上游连接失败返回 `502`。
7. 上游鉴权失败映射为 `401` 或 `403`。
8. 上游其他错误状态得到正确映射。
9. 上游返回非 JSON 内容时返回 `502`。
10. 所有错误响应和测试输出均不包含 API Key。

测试文件放置于 `tests/test_chat.py`。本期不设定全项目覆盖率阈值，但新增逻辑的成功路径和上述异常分支都必须被测试覆盖。

## Boundaries

### Always do

- 从环境变量读取 API Key。
- 验证调用方输入。
- 为上游请求设置超时。
- 在提交前运行自动化测试和语法检查。
- 保持 README 中的配置和调用示例与实现一致。

### Ask first

- 增加或升级第三方依赖。
- 改变公开接口路径或请求、响应结构。
- 将模型名、URL 或超时改成新的外部配置方案。
- 添加持久化、用户认证、限流或重试策略。

### Never do

- 将真实 API Key 写入源码、文档、测试、日志或 Git。
- 在自动化测试中调用真实模型接口。
- 向客户端泄露 Authorization 请求头或内部堆栈。
- 未经确认加入流式输出、多轮对话或模型选择等范围外功能。

## Success Criteria

- `POST /chat` 接受非空文本，并按规定调用阿里云 Responses API。
- 上游请求体严格包含 `model: qwen3-max` 和调用方提供的 `input`。
- 上游成功 JSON 响应被原样返回。
- API Key 仅从 `DASHSCOPE_API_KEY` 读取，仓库中不存在真实密钥。
- 输入错误、缺少配置、超时、连接失败、鉴权失败、上游错误和非 JSON 响应均按规格映射。
- 自动化测试不访问真实网络，并覆盖成功及规定的异常路径。
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q` 全部通过。
- `python3 -m compileall -q main.py tests` 全部通过。
- README 包含环境变量、启动命令和本地调用示例。

## Open Questions

无。范围变化时先更新本规格并重新审批。
