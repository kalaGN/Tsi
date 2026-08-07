# Project Overview

## Goal

本项目用于学习和验证 FastAPI、Textual 与外部模型接口集成。HTTP 提供无状态单轮文本调用，本地全屏 TUI 提供可持久化和恢复的唯一多轮会话；两者通过统一 Provider 层调用阿里云 Responses API 或 DeepSeek Chat Completions API。

## Project Shape

- 单 Git 仓库、单 Python 应用；HTTP 与 TUI 是两个独立入口。
- HTTP 对外提供统一 JSON；TUI 只在本地终端运行。
- Textual Worker 只防止单次异步请求阻塞界面，不是后台任务系统。
- 没有数据库、缓存、搜索、消息队列、多会话管理、微服务或定时任务；TUI 仅用本地 JSON 保存当前会话。

## Technology

版本真实来源为 `requirements.txt`：

| Component | Version | Usage |
|---|---:|---|
| Python | 3.11.5（`.venv` 已验证） | 运行时 |
| FastAPI | 0.125.0 | HTTP 应用和路由 |
| Pydantic | 1.10.19 | 请求与响应模型 |
| HTTPX | 0.28.1 | 异步模型调用和 MockTransport |
| Uvicorn | 0.49.0 | ASGI 开发服务器 |
| Pytest | 7.4.0 | 自动化测试 |
| Textual | 8.2.8 | TUI、异步 Worker 和无头测试 |
| python-dotenv | 1.2.2 | TUI 加载根目录 `.env` |

仓库没有 Provider SDK、lock 文件、`pyproject.toml`、Formatter、Lint、类型检查、构建或 CI 配置。

## Entrypoints and Modules

- `main.py`：Uvicorn 兼容入口。
- `app/application.py`：FastAPI 应用组装。
- `app/routers/chat.py`：`POST /chat` 请求与统一响应。
- `app/runtime/chat.py`：HTTP/TUI 共享用例、结果和错误语义。
- `app/services/llm/`：配置工厂、共享网络边界、阿里云与 DeepSeek Provider。
- `app/tui/`：Textual 应用、状态和模块启动入口。
- `tests/test_llm_providers.py`：Provider 协议与错误测试。
- `tests/test_chat.py`、`tests/test_chat_runtime.py`、`tests/test_tui.py`：对应交互边界测试。

## Confirmed Commands

```bash
.venv/bin/python --version
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn main:app --reload --env-file .env
.venv/bin/python -m app.tui
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q main.py app tests
.venv/bin/python -m pip check
git diff --check
```

## Current Boundaries

明确支持：

- 严格非空的单轮文本输入。
- 部署级选择 `aliyun` 或 `deepseek`，以及 Provider 专属模型覆盖。
- HTTP 固定返回 `{"output_text": "..."}`，TUI 展示同一文本。
- 中文输入、耗时统计、请求取消、`/clear`、`/quit`、Enter 和双击 Esc。
- 环境变量密钥、固定上游 URL、显式超时和脱敏错误分类。

明确不支持：

- 请求级 Provider/模型选择、流式、多会话管理、上下文压缩、工具调用、多 Agent 和多模态。
- 自动重试、降级、负载均衡、熔断、限流、用户认证授权和任务队列。
- 容器、反向代理、进程管理、CI/CD、结构化日志、Trace、指标、告警和正式健康检查。

## Dependency Changes

- 无需求不得新增或升级依赖。
- 变更前说明用途、替代方案、兼容性、安全和测试影响，并创建 Spec。
- 变更后更新依赖声明和文档，运行依赖检查与全量测试。
