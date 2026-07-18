# Tasks: Main 模块架构拆分

关联文档：

- [规格](../spec/2026-07-18-main-module-refactor.md)
- [实施计划](../plan/2026-07-18-main-module-refactor-plan.md)

## Task 1: 锁定保留和删除行为

- [x] 为 `GET /` 增加回归测试，锁定 `{"Hello": "World"}`。
- [x] 为 `GET /items/1` 增加预期 `404` 的删除行为测试。
- [x] 为 `PUT /items/1` 增加预期 `404` 的删除行为测试。
- [x] 增加 `main.app` 可导入测试。
- [x] 运行测试并确认 Items 删除测试因旧接口仍存在而失败。

Acceptance：

- 根接口保留行为有自动化测试保护。
- Items 的两个旧路径都有明确删除契约。
- 红灯来自旧 Items 路由仍存在，而非测试环境或语法错误。

Verify：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
```

Files：

- `tests/test_chat.py`

## Task 2: 创建包结构并迁移上游 Service

- [x] 创建 `app/__init__.py`。
- [x] 创建 `app/services/__init__.py`。
- [x] 创建 `app/services/aliyun_responses.py`。
- [x] 迁移上游 URL、模型、超时和 `request_upstream_response`。
- [x] 将测试 MockTransport patch 目标更新为新 Service 模块。
- [x] 确认所有 Chat 上游契约测试通过。

Acceptance：

- Service 接收普通字符串，返回状态码和 JSON 数据。
- Service 不依赖 Router、Application 或请求模型。
- 上游请求和错误映射行为保持不变。
- 测试不访问真实网络。

Verify：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests/test_chat.py
```

Files：

- `app/__init__.py`
- `app/services/__init__.py`
- `app/services/aliyun_responses.py`
- `tests/test_chat.py`
- `main.py`（迁移期间仅删除已移动的 Service 逻辑）

## Task 3: 创建 Chat Router

- [x] 创建 `app/routers/__init__.py`。
- [x] 创建 `app/routers/chat.py`。
- [x] 迁移 `ChatRequest` 和非空白校验。
- [x] 创建 `APIRouter` 并迁移 `POST /chat`。
- [x] Router 调用 Aliyun Service，不包含上游协议细节。

Acceptance：

- Chat 请求模型和路由位于 Router 模块。
- `/chat` 的路径、请求、成功响应和错误响应保持不变。
- Router 到 Service 的依赖为单向依赖。

Verify：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests/test_chat.py
```

Files：

- `app/routers/__init__.py`
- `app/routers/chat.py`
- `main.py`
- `tests/test_chat.py`

## Task 4: 创建 Application 并收缩兼容入口

- [x] 创建 `app/application.py`。
- [x] 实现 `create_app() -> FastAPI`。
- [x] 在 Application 中注册 `GET /` 和 Chat Router。
- [x] 导出 `app.application.app`。
- [x] 将根目录 `main.py` 收缩为仅导出 `app`。
- [x] 删除 `Item` 模型和全部 Items 路由。
- [x] 增加 `main.app is app.application.app` 一致性断言。

Acceptance：

- `main.py` 不包含业务逻辑、模型或路由实现。
- `uvicorn main:app` 导入路径保持可用。
- 根接口与 Chat 接口行为保持不变。
- 两个 Items 旧路径均返回 `404`。

Verify：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
python3 -c 'from main import app; assert app is not None'
```

Files：

- `app/application.py`
- `main.py`
- `tests/test_chat.py`

## Task 5: 同步架构知识库

- [x] 更新 `docs/knowledge/architecture.md` 的项目概览。
- [x] 更新组件职责和实际文件结构。
- [x] 记录 Application → Router → Service 的依赖方向。
- [x] 更新 Chat 请求链路。
- [x] 删除所有逻辑集中在 `main.py` 的过期描述。

Acceptance：

- 架构知识与实际代码目录和依赖方向一致。
- `main.py` 被描述为兼容入口。
- 文档不引入未实现的层次或组件。

Verify：

```bash
rg -n 'application|routers|services|main.py' docs/knowledge/architecture.md
```

Files：

- `docs/knowledge/architecture.md`

## Task 6: 完成质量与架构审查

- [x] 运行完整测试。
- [x] 运行全部 Python 文件语法检查。
- [x] 检查模块导入和循环依赖风险。
- [x] 确认 Items 代码无残留。
- [x] 确认未新增依赖、真实密钥或真实网络测试。
- [x] 按正确性、可读性、架构、安全和性能完成代码审查。

Acceptance：

- 所有自动化测试通过。
- 所有 Python 文件可编译。
- `rg` 搜索不到 `Item`、`read_item` 或 `update_item` 残留。
- Service 不依赖 Router 或 Application。
- Git 差异无空白或格式错误。
- 实现满足已审批规格且没有范围外抽象。

Verify：

```bash
python3 -m compileall -q main.py app tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
rg -n 'class Item|read_item|update_item|/items' main.py app tests
git diff --check
git status --short
```

Files：

- 本次全部变更文件（只读审查；问题回到对应任务修复）

## Execution Gate

- 每次只执行一个任务，并在进入下一任务前完成验证。
- 先观察 Items 删除契约测试失败，再删除旧接口。
- 迁移只改变代码位置和 Items 存在性，不改变 `/chat` 行为。
- 用户批准本任务清单后，方可进入 Phase 4（Implement）。
