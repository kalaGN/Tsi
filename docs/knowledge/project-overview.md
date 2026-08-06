# Project Overview

## Goal

本项目用于学习和验证 FastAPI、Textual 与外部模型接口集成。当前核心能力是通过 HTTP 或本地全屏 TUI 接收单轮文本，调用阿里云兼容模式 Responses API 的固定 `qwen3-max` 模型。

## Project Shape

- 单 Git 仓库、单 Python 应用；HTTP 服务和本地 TUI 是两个独立启动入口。
- HTTP 对外提供 JSON；TUI 只在本地终端运行。
- Textual Worker 只负责避免单次异步模型请求阻塞界面，不是后台任务系统。
- 没有多模块构建、微服务、后台 Worker、RPC、GraphQL、消息或定时任务。
- 没有数据库、缓存、搜索、对象存储和持久化会话。

## Technology

版本真实来源为 `requirements.txt`：

| Component | Version | Usage |
|---|---:|---|
| Python | 3.11.5（已验证环境） | 运行时 |
| FastAPI | 0.125.0 | HTTP 应用和路由 |
| Pydantic | 1.10.19 | 请求校验 |
| HTTPX | 0.28.1 | 异步上游 HTTP 调用和测试 Transport |
| Uvicorn | 0.49.0 | ASGI 开发服务器 |
| Pytest | 7.4.0 | 自动化测试 |
| Textual | 8.2.8 | 全屏终端界面、异步 Worker 和无头界面测试 |
| python-dotenv | 1.2.2 | TUI 启动时加载项目根目录 `.env` |

仓库仅有 `requirements.txt`，没有 lock 文件、`pyproject.toml`、Formatter、Lint、类型检查、构建或打包配置。

## Entrypoints and Modules

- `main.py`：Uvicorn 兼容入口，仅导出应用对象。
- `app/application.py`：创建 FastAPI、注册根路由和 Chat Router。
- `app/routers/chat.py`：`POST /chat`、请求 Schema 和 HTTP 响应。
- `app/runtime/chat.py`：HTTP/TUI 共享的单轮用例、结果和错误语义。
- `app/services/aliyun_responses.py`：阿里云请求、超时、响应解析和 Provider 异常。
- `app/tui/`：Textual 应用、状态和 `python3 -m app.tui` 入口。
- `tests/test_chat.py`：HTTP 契约与上游隔离测试。
- `tests/test_chat_runtime.py`：Runtime 单元测试。
- `tests/test_tui.py`：Textual 无头交互测试。

## Confirmed Commands

这些命令要求当前 `python3` 指向已安装依赖的 Python 3.11 环境：

```bash
python3 --version
python3 -m pip install -r requirements.txt
python3 -m uvicorn main:app --reload --env-file .env
python3 -m app.tui
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
python3 -m compileall -q main.py app tests
python3 -m pip check
git diff --check
```

当前机器已验证 `/Users/wangfei/anaconda3/bin/python3` 为 Python 3.11.5 且可运行全量测试；默认 `/usr/bin/python3` 为 3.9.6 且未安装依赖。Anaconda 共享环境的 `pip check` 存在多项全局包冲突，因此不能视为干净、可复现环境。团队环境管理方式待确认。

## Current Boundaries

明确支持：

- 非空单轮文本输入。
- 固定 `qwen3-max`。
- 成功 JSON 原样返回。
- 本地全屏 TUI、多行输入、非流式响应展示和请求取消。
- `/clear`、`/quit`、Ctrl+S 和 Ctrl+C；不提供 `/help`、`/chat` TUI 命令。
- API Key 环境变量读取。
- 上游连接、超时、鉴权、状态码和 JSON 错误分类。

明确不在当前实现中：

- 流式响应、多轮记忆、会话持久化、工具调用、多 Agent、模型选择和多模态输入。
- 数据持久化、用户认证授权、限流、重试、熔断和任务队列。
- 容器、反向代理、进程管理、CI/CD 和生产部署配置。
- 结构化日志、Tracing、指标、告警和正式健康检查。

## Dependency Changes

- 无需求不得新增或升级依赖。
- 变更前说明用途、替代方案、兼容性、安全和测试影响，并创建 Spec。
- 变更后更新 `requirements.txt`、相关文档，运行依赖检查和全量测试。
