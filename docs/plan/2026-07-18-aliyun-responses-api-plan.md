# Plan: 阿里云 Responses API 对接

关联规格：[阿里云 Responses API 对接](../spec/2026-07-18-aliyun-responses-api.md)

## Implementation Approach

在现有单文件 FastAPI 应用基础上完成首版对接：请求模型、上游调用函数和 `/chat` 路由均保留在 `main.py`，避免为当前规模引入不必要的模块层级。测试集中放在 `tests/test_chat.py`，通过替换上游 HTTP 调用来隔离真实网络。

实现采用异步 HTTPX 客户端：

1. 路由使用 Pydantic 校验非空输入。
2. 上游调用前读取 `DASHSCOPE_API_KEY`，缺失时立即返回 `503`。
3. 构造固定模型和用户输入组成的请求体。
4. 调用阿里云 Responses API，并分类映射超时、连接失败、HTTP 错误和无效 JSON。
5. 成功时使用 FastAPI JSON 响应原样返回上游 JSON 和状态码。

## Components

### 1. Dependency declaration

文件：`requirements.txt`

- 声明 FastAPI、Uvicorn、HTTPX、Pytest 等项目所需依赖。
- 使用与当前环境兼容的版本范围，避免无依据的大版本升级。
- HTTPX 是新增的生产依赖；Pytest 是新增的测试依赖。

依赖关系：所有实现与测试任务的前置步骤。

### 2. Request validation

文件：`main.py`

- 新增 `ChatRequest` Pydantic 模型。
- `input` 必须为非空字符串。
- 空字符串是否包含纯空白需在实现中显式拒绝，避免仅依赖 `min_length=1` 接受 `"   "`。

依赖关系：`POST /chat` 路由依赖该模型。

### 3. Upstream client function

文件：`main.py`

- 集中定义上游 URL、模型名和超时常量。
- 从 `DASHSCOPE_API_KEY` 环境变量读取密钥。
- 使用 `httpx.AsyncClient` 发出请求。
- 仅设置必要请求头，不手动设置 `Host`、`Connection` 或 Apifox User-Agent。
- 将异常分类转换为 FastAPI `HTTPException`。
- 解析并返回 JSON；无法解析时返回 `502`。

依赖关系：依赖 HTTPX；供 `/chat` 路由调用。

### 4. Public chat endpoint

文件：`main.py`

- 新增 `POST /chat`。
- 接收 `ChatRequest`。
- 异步调用上游客户端函数。
- 使用上游成功状态码和 JSON 构建响应。

依赖关系：依赖请求模型与上游客户端函数。

### 5. Automated tests

文件：`tests/test_chat.py`

- 通过 monkeypatch 或 HTTPX MockTransport 模拟上游，不访问真实网络。
- 验证请求 URL、Authorization、模型名和输入。
- 覆盖成功、输入校验、缺少密钥、超时、连接失败、鉴权失败、其他 HTTP 错误及非 JSON 响应。
- 验证错误输出不包含 API Key。

依赖关系：依赖完整接口实现。

### 6. Usage documentation

文件：`README.md`

- 增加 `DASHSCOPE_API_KEY` 配置说明。
- 增加 `/chat` 的 curl 示例。
- 标注真实密钥不得提交到 Git。

依赖关系：应在接口契约稳定且测试通过后更新。

### 7. Repository hygiene

文件：`.gitignore`

- 排除 `__pycache__/`、`*.py[cod]`、虚拟环境、测试缓存及 `.env`。
- 防止缓存文件和本地密钥配置进入版本控制。

依赖关系：可与依赖声明并行完成，但必须在最终检查前完成。

## Implementation Order

1. 添加 `.gitignore` 和依赖声明。
2. 先编写 `/chat` 的失败测试与成功测试，使其在未实现时失败。
3. 实现请求模型及空白输入校验。
4. 实现上游异步调用与错误映射。
5. 实现 `/chat` 路由并使测试通过。
6. 补齐 README 使用说明。
7. 运行完整测试、语法检查和仓库敏感信息检查。

该顺序确保接口行为由已审批规格和测试约束，再逐步补齐最小实现。

## Parallelization

当前改动规模小，核心工作集中在 `main.py` 与同一组接口测试，顺序实现更容易保持契约一致。

可独立处理的工作：

- `.gitignore` 与依赖声明可以并行准备。
- README 更新可在接口实现期间准备，但只能在最终行为确认后定稿。

必须顺序处理的工作：

- 测试契约先于业务实现。
- 请求模型、上游客户端函数先于路由集成。
- 完整验证先于交付。

## Risks and Mitigations

### 上游响应结构未来变化

风险：如果代码绑定具体响应字段，上游升级可能导致失败。

缓解：本期将成功 JSON 作为不透明对象原样返回，不依赖具体回答字段。

### HTTP 客户端难以稳定模拟

风险：直接在路由内部创建客户端可能使测试依赖实现细节。

缓解：将上游调用集中到独立函数，并以明确边界替换或使用 HTTPX MockTransport。

### 密钥泄露

风险：密钥可能进入代码、异常文本、README、测试或 Git。

缓解：仅从环境变量读取；测试使用明显的假密钥；错误响应不回显请求头；`.gitignore` 排除 `.env`；最终运行敏感信息搜索。

### 超时配置不合适

风险：模型响应时间较长，超时过短导致误判，过长则占用连接。

缓解：使用集中定义的显式超时，首版采用适合大模型响应的保守值；后续根据实际观测调整，调整前更新规格。

### 上游错误响应含敏感信息

风险：完全透传错误体可能暴露内部信息。

缓解：成功响应原样返回；失败响应仅保留安全、可诊断的错误说明，不透传请求信息。

### 新增依赖未获确认

风险：规格边界要求新增第三方依赖前确认。

缓解：计划审批同时明确申请加入 HTTPX 与 Pytest；未获审批不进入实现。

## Verification Checkpoints

### Checkpoint 1: Test contract

- `/chat` 场景测试已写入。
- 未实现接口时测试按预期失败，而不是因测试自身错误失败。

验证命令：

```bash
python3 -m pytest -q
```

### Checkpoint 2: Core implementation

- 成功路径、输入校验和所有错误映射测试通过。
- 测试期间没有真实网络请求。

验证命令：

```bash
python3 -m pytest -q
```

### Checkpoint 3: Static and documentation verification

- Python 文件可编译。
- README 示例与接口一致。
- `.gitignore` 覆盖缓存和 `.env`。

验证命令：

```bash
python3 -m compileall -q main.py tests
git status --short
```

### Checkpoint 4: Secret and scope audit

- 仓库中不存在真实 API Key。
- 实现没有流式输出、多轮历史、模型选择等范围外功能。
- 自动化测试不访问阿里云域名。

验证命令：

```bash
rg -n "Authorization|Bearer|DASHSCOPE_API_KEY|llm-h2k07hgnp4aylibi" . \
  -g '!__pycache__/**' \
  -g '!.git/**'
```

搜索结果须逐项人工确认，仅包含环境变量引用、假密钥、上游 URL和文档示例。

## Plan Exit Criteria

- 技术组件及其依赖已明确。
- 实现顺序、测试优先策略及验证检查点已明确。
- 风险及缓解措施已明确。
- 用户批准新增 HTTPX 与 Pytest 依赖。
- 用户审批本计划后，方可进入 Phase 3（Tasks）。
