# Plan: Main 模块架构拆分

关联规格：[Main 模块架构拆分](../spec/2026-07-18-main-module-refactor.md)

## Implementation Approach

采用小步迁移而非一次性重写：先补齐公开行为测试，再创建新的 Service、Router 和 Application 模块，最后将根目录 `main.py` 收缩为兼容入口。每个阶段都运行测试，确保问题能定位到最近一次移动。

目标依赖方向：

```text
main.py
  -> app.application
       -> app.routers.chat
            -> app.services.aliyun_responses
```

Service 不反向依赖 Router 或 Application；Router 不负责上游协议细节；Application 只负责应用组装和根路由。

## Components

### 1. Regression contract

文件：`tests/test_chat.py`

- 增加 `GET /` 保留行为测试。
- 增加 `GET /items/1` 和 `PUT /items/1` 返回 `404` 的删除行为测试。
- 将 HTTPX MockTransport 的 patch 目标从旧 `main` 模块内部实现迁移到新的 Service 模块。
- 保持现有 13 个 Chat 契约测试覆盖。

### 2. Aliyun Responses service

文件：`app/services/aliyun_responses.py`

- 移入上游 URL、模型和超时常量。
- 移入 `request_upstream_response`。
- 保持环境变量读取、请求结构和错误映射不变。
- 模块对外只暴露路由所需的调用函数；测试可以访问常量核对请求契约。

### 3. Chat router

文件：`app/routers/chat.py`

- 移入 `ChatRequest` 和非空白校验。
- 创建 `APIRouter` 并注册 `POST /chat`。
- 调用 Service 并构造原样 JSON 响应。

### 4. Application assembly

文件：`app/application.py`

- 提供 `create_app() -> FastAPI`。
- 注册 `GET /`。
- 注册 Chat Router。
- 导出模块级 `app`，供兼容入口引用。

### 5. Compatibility entrypoint

文件：`main.py`

- 删除原有模型、路由和上游逻辑。
- 仅执行 `from app.application import app`。
- 保持 `main:app` 启动路径不变。

### 6. Knowledge synchronization

文件：`docs/knowledge/architecture.md`

- 更新组件列表、目录结构和请求链路。
- 记录 Application → Router → Service 依赖方向。
- 删除“所有逻辑集中在 main.py”的过期描述。

## Implementation Order

1. 补充根接口和 Items 删除契约测试，并确认 Items 删除测试在当前实现下失败。
2. 创建 `app/`、`app/services/` 和 `app/routers/` 包结构。
3. 迁移阿里云 Service，调整 MockTransport patch 边界，运行 Chat 测试。
4. 迁移 Chat 请求模型和路由，运行 Chat 测试。
5. 创建 Application 组装模块和根路由。
6. 收缩 `main.py` 为兼容入口，并确认 Items 路由消失。
7. 更新架构知识库，执行完整质量检查。

## Verification Checkpoints

### Checkpoint 1: Removal test red phase

在删除 Items 前：

- `GET /` 测试通过。
- 两个 Items `404` 测试按预期失败，证明测试能检测旧接口仍存在。

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
```

### Checkpoint 2: Service migration

- Chat 成功、超时、连接、鉴权、非成功状态和非 JSON 测试通过。
- MockTransport patch 的是新 Service 边界。
- 没有真实网络请求。

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests/test_chat.py
```

### Checkpoint 3: Router and application migration

- `GET /` 和全部 Chat 测试通过。
- Items 旧路径返回 `404`。
- `main.app` 与 `app.application.app` 是同一个应用对象。

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
```

### Checkpoint 4: Final quality gate

```bash
python3 -m compileall -q main.py app tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
git diff --check
git status --short
```

人工检查：

- `main.py` 无业务逻辑。
- Service 不依赖 FastAPI Router 或 Application。
- 未保留不可达的 Items 代码。
- 架构知识与实际文件一致。
- 未新增依赖、密钥或真实网络测试。

## Risks and Mitigations

### Mock patch 边界失效

风险：测试仍 patch `main.httpx`，迁移后可能误发真实网络请求。

缓解：先将测试显式改为 patch `app.services.aliyun_responses.httpx.AsyncClient`，并保留请求断言。

### 循环依赖

风险：Service 引用 Router 模型，Router 同时引用 Service。

缓解：Service 只接收普通字符串并返回 `(status_code, JSON data)`；请求模型仅属于 Router。

### 启动入口破坏

风险：移动应用对象后 `uvicorn main:app` 无法导入。

缓解：根目录保留薄 `main.py`，并增加对象导入一致性测试。

### 行为在重构中漂移

风险：拆分时顺手修改错误信息、超时或响应结构。

缓解：复用现有契约测试，禁止本次修改 `/chat` 行为和上游常量。

### 过度架构

风险：为小项目引入 Config、Repository、Manager 等无收益层次。

缓解：本期限制为 Application、Router、Service 三个职责，不新增依赖和额外抽象。

## Parallelization

本次文件之间存在明确迁移依赖，且改动规模较小，顺序执行优于多 Agent 并行修改。

- 测试契约必须先于迁移。
- Service 必须先于 Router。
- Router 必须先于 Application 组装。
- 知识库更新可以提前准备，但只能在最终结构确认后定稿。

## Plan Exit Criteria

- 模块职责、依赖方向和迁移顺序已明确。
- 每个迁移阶段有独立验证点。
- Items 删除和启动兼容性有自动化测试保护。
- 风险和缓解措施已明确。
- 用户审批本计划后，方可进入 Phase 3（Tasks）。
