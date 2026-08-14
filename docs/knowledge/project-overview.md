# Project Overview

## Goal

本项目用于学习和验证 FastAPI、Textual、外部模型流式接口与受控 Function Calling。HTTP 提供聚合 JSON 的无状态文本调用和只读时间工具；本地全屏 TUI 提供流式展示、可恢复单会话，以及绑定启动目录的项目读取、审批修改、固定检查和撤销闭环。

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
- `app/runtime/tool_loop.py`：有界模型步骤和串行工具执行编排。
- `app/services/llm/`：配置工厂、共享网络边界、阿里云与 DeepSeek Provider。
- `tools/`：Provider 中立契约、Registry、`get_current_time`、Workspace 策略、文件/Git 工具和固定项目检查。
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
.venv/bin/python -m compileall -q main.py app tools tests
.venv/bin/python -m pip check
git diff --check
```

## Current Boundaries

明确支持：

- 严格非空的单轮文本输入。
- 部署级选择 `aliyun` 或 `deepseek`，以及 Provider 专属模型覆盖。
- 两家上游均使用 SSE；HTTP 聚合后固定返回 `{"output_text": "..."}`，TUI 增量展示纯文本并在完成后用同一原文渲染 Assistant Markdown。
- 中文输入、`Cmd+A` / `Ctrl+A` 全选输入、Esc 清空输入、最终耗时统计、请求中动画与实时耗时、请求取消、`/clear`、`/quit`、Enter 和双击 Esc。
- TUI 启动目录直属 `AGENTS.md` 的 32 KiB UTF-8 有界读取，以及不持久化的 Provider 标准 system 消息。
- 上下键输入历史、草稿恢复，以及从成功 Session user 消息恢复历史。
- 用户消息以不解析 Markdown 的全宽背景卡片展示；Assistant 支持标题、列表、引用、链接、表格和代码块的 Rich Markdown 展示；系统和错误保持纯文本。
- 最终消息和流式临时文本支持鼠标选择，并通过 `Cmd+C` / `Ctrl+C` 复制渲染后的可见文字；对话记录还可双击立即复制当前渲染行。
- 环境变量密钥、固定上游 URL、显式超时和脱敏错误分类。
- HTTP 自动执行只读当前时间工具；TUI 自动执行 Workspace 只读工具并审批每次写入/撤销。
- TUI 支持结构化 create/replace、哈希冲突保护、原子批次、固定项目检查和进程内 LIFO 撤销。
- request ID 关联的结构化模型、HTTP 和工具日志。
- 完整上游请求日志包含实际 system 消息；HTTP `/chat` 不加载本地项目规则。

明确不支持：

- HTML、远程图片、Mermaid、Markdown 代码执行、HTTP SSE、请求级 Provider/模型选择、多会话管理、上下文压缩、任意 Shell、MCP、动态插件、多 Agent 和多模态。
- 文件删除、移动、重命名、自动依赖安装、Git Commit/Tag/Push、热加载和跨重启撤销。
- 自动重试、降级、负载均衡、熔断、限流、用户认证授权和任务队列。
- 容器、反向代理、进程管理、CI/CD、Trace、指标、告警、远程日志采集和正式健康检查。

## Dependency Changes

- 无需求不得新增或升级依赖。
- 变更前说明用途、替代方案、兼容性、安全和测试影响，并创建 Spec。
- 变更后更新依赖声明和文档，运行依赖检查与全量测试。
