# Spec: Main 模块架构拆分

## Objective

将当前 `main.py` 中混合的应用创建、请求模型、路由和阿里云上游调用按职责拆分，使应用入口保持稳定，同时降低模块耦合并提升后续维护性。

本次重构同时删除不再需要的 Items 示例接口和 `Item` 模型。`POST /chat` 的公开契约、错误映射和启动命令必须保持不变。

## Scope

包含：

- 新建 `app/` Python 包。
- 将 FastAPI 应用创建和路由注册移入应用模块。
- 将 `/chat` 路由及 `ChatRequest` 移入 Chat 路由模块。
- 将阿里云 Responses API 调用移入 Service 模块。
- 保留根接口 `GET /`。
- 删除 `GET /items/{item_id}`、`PUT /items/{item_id}` 和 `Item` 模型。
- 保留根目录 `main.py` 作为 Uvicorn 兼容入口。
- 更新测试和架构知识库。

不包含：

- 引入配置框架或依赖注入容器。
- 引入 Repository、数据库或持久化层。
- 修改 `/chat` 请求、响应或错误结构。
- 修改阿里云上游地址、模型或超时。
- 增加新的业务接口。

## Public Behavior

### 保留

- `GET /` 返回 `{"Hello": "World"}`。
- `POST /chat` 接收非空文本并原样返回上游成功 JSON。
- `/docs` 和 `/redoc` 继续可用。
- 启动命令保持不变：

```bash
python3 -m uvicorn main:app --reload --env-file .env
```

### 删除

- `GET /items/{item_id}`。
- `PUT /items/{item_id}`。
- 删除后访问上述路径返回 `404 Not Found`。

## Tech Stack

- Python 3.11
- FastAPI 0.125.x
- Pydantic 1.10.x
- HTTPX 0.28.x
- Pytest 7.4.x

本次不新增或升级第三方依赖。

## Commands

启动：

```bash
python3 -m uvicorn main:app --reload --env-file .env
```

测试：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
```

语法检查：

```bash
python3 -m compileall -q main.py app tests
```

## Project Structure

```text
demo/
├── app/
│   ├── __init__.py
│   ├── application.py              # 创建 FastAPI、根路由、注册业务路由
│   ├── routers/
│   │   ├── __init__.py
│   │   └── chat.py                 # ChatRequest 与 POST /chat
│   └── services/
│       ├── __init__.py
│       └── aliyun_responses.py     # 上游配置、请求与错误映射
├── main.py                         # 仅从 app.application 导出 app
├── tests/
│   └── test_chat.py                # Chat、根路由及 Items 删除回归测试
└── docs/
    └── knowledge/
        └── architecture.md         # 更新后的架构事实
```

边界说明：

- `application.py` 负责组装应用，不包含上游业务逻辑。
- `routers/chat.py` 负责 HTTP 输入输出和请求模型，不实现网络调用细节。
- `services/aliyun_responses.py` 不依赖路由模块，负责上游集成。
- `main.py` 不包含路由、模型或业务逻辑。

## Code Style

- 使用绝对包导入，例如 `from app.routers.chat import router`。
- 路由模块导出语义明确的 `router`。
- Service 函数使用类型标注和异步接口。
- 不创建只有转发作用的额外 Manager、Repository 或 Client 层。
- 保持现有异常分类和安全错误信息。

示例：

```python
from fastapi import FastAPI

from app.routers.chat import router as chat_router


def create_app() -> FastAPI:
    application = FastAPI()
    application.include_router(chat_router)
    return application


app = create_app()
```

## Testing Strategy

重构前后的公开行为通过自动化测试锁定：

1. `GET /` 继续返回原有 JSON。
2. `POST /chat` 的成功请求、输入校验和全部错误映射继续通过。
3. 测试继续通过 HTTPX MockTransport 隔离真实网络。
4. `GET /items/1` 返回 `404`。
5. `PUT /items/1` 返回 `404`。
6. `main.app` 可被 Uvicorn 兼容导入。
7. 测试和运行代码不再依赖旧 `main.httpx` 或旧模块内部常量。

## Boundaries

### Always do

- 在移动代码前用现有测试锁定 `/chat` 行为。
- 每次模块移动后运行目标测试。
- 使用最小职责拆分，保持依赖方向为 Application → Router → Service。
- 更新架构知识库和相关命令。

### Ask first

- 改变 `/chat` 契约、模型、上游 URL 或超时。
- 增加第三方依赖。
- 继续拆分配置层、领域层或客户端抽象。
- 删除根接口 `/`。

### Never do

- 为兼容旧测试而保留无业务用途的 Items 代码。
- 调用真实阿里云接口执行自动化测试。
- 将真实密钥写入代码、测试、日志或 Git。
- 使用破坏性 Git 命令回滚现有变更。

## Success Criteria

- 根目录 `main.py` 只负责导出 FastAPI 应用。
- `/chat` 路由和请求模型位于 `app/routers/chat.py`。
- 阿里云集成位于 `app/services/aliyun_responses.py`。
- Items 路由和模型完全删除，两个旧路径均返回 `404`。
- `GET /` 和 `POST /chat` 的保留行为通过测试。
- 自动化测试不访问真实网络且全部通过。
- `python3 -m compileall -q main.py app tests` 通过。
- 架构知识库与实际目录一致。
- 未新增第三方依赖或范围外抽象。

## Open Questions

无。若需要删除根接口或进一步拆分配置模块，先更新本规格并重新审批。
