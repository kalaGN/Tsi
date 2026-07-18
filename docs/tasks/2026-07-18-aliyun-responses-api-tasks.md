# Tasks: 阿里云 Responses API 对接

关联文档：

- [规格](../spec/2026-07-18-aliyun-responses-api.md)
- [实施计划](../plan/2026-07-18-aliyun-responses-api-plan.md)

## Task 1: 建立依赖与仓库忽略规则

- [x] 创建 `requirements.txt`，声明 FastAPI、Uvicorn、HTTPX 和 Pytest 依赖。
- [x] 创建 `.gitignore`，排除 Python 缓存、虚拟环境、Pytest 缓存和 `.env`。
- [x] 确认依赖可被当前 Python 环境导入。

Acceptance：

- `requirements.txt` 包含运行和测试所需依赖，版本约束与当前 Python 3.11 环境兼容。
- `.env`、`__pycache__/`、`*.py[cod]`、`.pytest_cache/` 和常见虚拟环境目录不会被 Git 跟踪。
- 不提交任何真实 API Key。

Verify：

```bash
python3 -c 'import fastapi, httpx, pytest, uvicorn'
git check-ignore __pycache__/ .env .pytest_cache/
```

Files：

- `requirements.txt`
- `.gitignore`

## Task 2: 编写接口契约测试

- [x] 创建 `tests/test_chat.py`。
- [x] 编写成功响应测试，验证 URL、Bearer Token、模型名和输入。
- [x] 编写缺少字段、空字符串和纯空白字符串的输入校验测试。
- [x] 编写缺少 `DASHSCOPE_API_KEY` 的测试。
- [x] 编写超时、连接失败、鉴权失败、其他 HTTP 错误和非 JSON 响应测试。
- [x] 确认测试通过 mock 边界隔离真实网络。

Acceptance：

- 测试覆盖规格定义的成功和错误场景。
- 测试使用明显的假密钥，不访问阿里云网络。
- 在业务实现完成前，测试因缺少 `/chat` 行为而按预期失败。
- 失败原因来自未实现行为，而不是测试语法或测试环境错误。

Verify：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
```

Files：

- `tests/test_chat.py`

## Task 3: 实现输入模型与配置检查

- [x] 在 `main.py` 新增 `ChatRequest`。
- [x] 拒绝空字符串和纯空白字符串。
- [x] 集中定义上游 URL、固定模型名和显式超时。
- [x] 从 `DASHSCOPE_API_KEY` 读取 API Key。
- [x] 缺少或为空时返回 `503 Service Unavailable`，且不调用上游。

Acceptance：

- 有效文本通过验证。
- 缺少字段、类型错误、空字符串和纯空白字符串返回 `422`。
- API Key 不出现在源码默认值、日志或错误响应中。
- 缺少 API Key 时返回 `503`。

Verify：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests/test_chat.py
```

Files：

- `main.py`
- `tests/test_chat.py`（仅在测试契约需要校正时修改）

## Task 4: 实现异步上游调用与错误映射

- [x] 使用 `httpx.AsyncClient` 调用阿里云 Responses API。
- [x] 请求头仅包含 Bearer 鉴权、JSON Content-Type 和 Accept。
- [x] 请求体固定使用 `qwen3-max`，并携带调用方输入。
- [x] 映射超时为 `504`。
- [x] 映射连接失败为 `502`。
- [x] 安全映射 `401`、`403` 和其他上游非成功状态。
- [x] 上游成功但响应不是 JSON 时返回 `502`。
- [x] 确保所有错误均不泄露 API Key 或内部堆栈。

Acceptance：

- 上游请求与规格中的 URL、请求头和请求体一致。
- 所有规定的异常类别得到正确 HTTP 状态码。
- 自动化测试完全隔离真实网络。

Verify：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests/test_chat.py
```

Files：

- `main.py`
- `tests/test_chat.py`（仅在测试契约需要校正时修改）

## Task 5: 实现 `POST /chat` 路由

- [x] 新增异步 `POST /chat` 路由。
- [x] 接收 `ChatRequest` 并调用上游函数。
- [x] 原样返回上游成功 JSON 和状态码。
- [x] 保留当前 `/` 和 `/items/{item_id}` 行为。

Acceptance：

- `/chat` 满足已审批 API Contract。
- 成功响应不依赖上游 JSON 的具体字段结构。
- 现有示例接口未发生行为回归。
- 全部接口测试通过。

Verify：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
python3 -m compileall -q main.py tests
```

Files：

- `main.py`
- `tests/test_chat.py`

## Task 6: 更新使用文档

- [x] 在 `README.md` 中说明 `DASHSCOPE_API_KEY` 环境变量。
- [x] 增加 `/chat` 的本地 curl 调用示例。
- [x] 明确真实密钥不得写入代码或提交到 Git。

Acceptance：

- 新用户可按 README 完成配置、启动和调用。
- 示例请求与实际接口结构一致。
- README 不包含真实 API Key。

Verify：

```bash
rg -n 'DASHSCOPE_API_KEY|POST /chat|127\.0\.0\.1:8000/chat' README.md
```

Files：

- `README.md`

## Task 7: 完成最终质量与安全验证

- [x] 运行完整自动化测试。
- [x] 运行 Python 语法检查。
- [x] 检查 Git 忽略规则和工作区状态。
- [x] 搜索密钥、Authorization 和上游地址引用并逐项审核。
- [x] 确认没有实现流式输出、多轮历史、模型选择等范围外功能。
- [x] 按五个维度执行代码审查：正确性、可读性、架构、安全和性能。

Acceptance：

- 所有测试和语法检查通过。
- 没有真实网络调用或真实密钥。
- 所有变更符合规格与项目规则。
- 没有未解释的缓存或生成文件进入 Git 范围。

Verify：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
python3 -m compileall -q main.py tests
git status --short
rg -n 'Authorization|Bearer|DASHSCOPE_API_KEY|llm-h2k07hgnp4aylibi' . \
  -g '!__pycache__/**' \
  -g '!.git/**'
```

Files：

- 所有本次变更文件（只读审查；发现问题时回到对应任务修复）

## Execution Gate

- 每次只执行一个任务。
- 每个任务完成后先运行其验证命令，再进入下一任务。
- 实现阶段遵循测试先行：先观察契约测试失败，再编写最小实现使其通过。
- 用户批准本任务清单后，方可进入 Phase 4（Implement）。
