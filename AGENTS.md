# AI 开发指南

## 项目概述

- 项目正式名称为 `Tsi 助手`；这是一个轻量模型调用项目，上游统一使用流式协议，对外提供聚合 JSON 的无状态 FastAPI HTTP 接口和支持增量展示、可恢复单会话的本地 Textual TUI。
- Python 3.11；依赖版本以 `requirements.txt` 为准。
- 当前不是多 Agent 系统，不含数据库、缓存、消息队列或后台 Worker；TUI 仅用本地 JSON 持久化唯一当前会话，Textual Worker 只承担异步请求。

## 架构

- 启动入口：`main.py`，仅导出 `app.application.app`。
- 应用组装：`app/application.py`。
- HTTP 路由与请求校验：`app/routers/`。
- HTTP/TUI 共享模型调用与中立错误，TUI Session、系统提示词读取、Skill Runtime 及存储也位于：`app/runtime/`。
- 根目录工具契约、Registry、Workspace 策略、固定检查和 Codex 兼容 Skill 快照：`tools/`。
- 多模型配置、协议适配和 Provider 错误：`app/services/llm/`。
- 终端界面与启动入口：`app/tui/`。
- TUI 可选择消息组件：`app/tui/widgets.py`。
- TUI 消息角色渲染与流式文本缓冲：`app/tui/transcript.py`。
- TUI 命令候选组件：`app/tui/command_palette.py`；布局样式：`app/tui/styles/*.tcss`。
- 测试：`tests/`。
- 真实依赖方向：`HTTP/TUI → runtime → tools + services.llm → Aliyun/DeepSeek`；HTTP 入口为 `main → application → router`。
- 详细事实：[架构知识](docs/knowledge/architecture.md)。

## 环境与命令

使用仓库本地 `.venv`，当前已验证为 Python 3.11.5 且依赖完整。

- 环境确认：`.venv/bin/python --version`
- 安装：`.venv/bin/python -m pip install -r requirements.txt`
- 启动：`.venv/bin/python -m uvicorn main:app --reload --env-file .env`
- TUI：`.venv/bin/python -m app.tui`
- 测试：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q`
- 语法检查：`.venv/bin/python -m compileall -q main.py app tools tests`
- 依赖检查：`.venv/bin/python -m pip check`
- 差异检查：`git diff --check`

仓库没有配置 Formatter、Lint、类型检查、构建打包或 CI 命令，不得虚构对应门禁。

## 文档索引

- 规则索引：[docs/rules/README.md](docs/rules/README.md)
- 项目规则：[docs/rules/harness-engineering-rules.md](docs/rules/harness-engineering-rules.md)
- 需求 Spec：[docs/spec/README.md](docs/spec/README.md)
- 设计计划：[docs/plan/README.md](docs/plan/README.md)
- 任务清单：[docs/tasks/README.md](docs/tasks/README.md)
- 知识索引：[docs/knowledge/README.md](docs/knowledge/README.md)
- 项目审计：[docs/knowledge/repository-audit.md](docs/knowledge/repository-audit.md)

## 变更前

- 先读相关源码、测试、已确认 Spec、项目 Rules 和相关 Knowledge。
- 报告需求、文档与代码冲突，不得静默选择。
- 公开 API、外部依赖、架构、安全或性能变更必须先写 Spec 并等待确认。
- 新增或升级依赖前先说明必要性、影响和替代方案。

## 变更后

- 代码增加必要注释，用于解释非显而易见的意图、边界、兼容原因和关键取舍；不要逐行复述代码。
- 生成或更新的项目文档统一使用中文，包括标题、章节名和说明文字；代码标识、命令、协议名及专有名词按原文保留。
- 新功能与缺陷修复同步覆盖正常、异常和边界测试。
- 运行真实可用的语法检查、全量测试、依赖检查和 `git diff --check`。
- 同步更新受影响的 Spec、任务和 Knowledge。
- Git Commit 使用 `type: 中文描述` 格式，例如 `feat: 增加终端对话入口`。
- 未经用户明确要求，不创建 Git Commit。

## 硬性边界

- 不擅自改变 `/chat` 请求、成功响应或错误映射等公开契约。
- `/chat` 当前成功响应固定为 `{"output_text": "..."}`，Router 和 TUI 不得解析或暴露 Provider 原始响应。
- 工具只能从根目录 `tools/` 显式注册；HTTP 仅自动执行无副作用工具。TUI 允许经完整 Diff 审批的结构化文件创建、精确替换和 LIFO 撤销；Skill 脚本仅能通过逐次审批的 `run_skill_script` 执行，不得提供任意命令、动态 import、删除/移动工具或数据库写操作。
- TUI 从启动目录 `.agents/skills/*/SKILL.md` 加载 Codex 兼容项目 Skill；`install_skill` 只可经逐次审批从公开 GitHub 目录或当前用户 `~/.codex/skills` 直属目录安装且下一次请求生效。Skill 文本和扩展字段不能注册工具、扩大权限或绕过审批，HTTP 不加载或安装 Skill。
- 密钥仅从环境变量读取；禁止进入代码、文档、日志、测试或 Git。
- 不无需求增加层级、服务、基础设施或第三方依赖。
- 自动化测试禁止调用真实付费或生产外部服务。
- 不删除或弱化测试来制造通过结果。
- 数据库、部署、认证授权、重试、性能等未落地能力不得描述为已实现。
- 文档命名与维护规则以规则索引为准；保持现有 `docs/` 目录结构不变。
