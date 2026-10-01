# Tsi 助手开发指南

- Python 3.11 项目，提供共享 Runtime 的本机 FastAPI Web UI 与 Textual TUI；当前不是多 Agent 系统。
- Web UI 入口：`main.py → app/application.py → app/webui/ → app/runtime/`，仅限本机，Workspace 写操作逐次审批。
- TUI：`python -m app.tui → app/tui/ → app/runtime/`。
- 对话先经 `task_preflight` 预判 direct/planned/clarify；planned 需用户确认计划后才执行（详见[编排](docs/knowledge/orchestration.md)）。
- 安装：`.venv/bin/python -m pip install -r requirements.txt`；启动：`.venv/bin/python -m app.tui` 或 `.venv/bin/python -m uvicorn main:app --reload --env-file .env`。
- 迭代时运行与改动直接相关的测试；提交、发布或跨模块 Runtime 变更前执行：`.venv/bin/python -m compileall -q main.py app tools tests`；`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q`；`.venv/bin/python -m pip check`；`git diff --check`。
- 按需读取相关源码、测试和文档；仅当冲突会改变范围、公开行为、安全或成本时等待确认，其余采用最小可逆方案并说明。
- 新项目或公开契约、依赖、架构、安全、性能策略的显著变更，在需求尚未确认时先写简短 Spec 并一次性确认；确认后持续实现至验收。
- 必要注释只解释非显而易见的意图与边界；行为变更补测试，公开行为、架构、配置或运维方式变化时同步中文文档。
- Web UI 与 TUI 不得暴露 Provider 原始响应；对话以有界 NDJSON 事件流式返回。
- 工具仅从 `tools/` 显式注册，按 `ToolEffect` 分级：`READ_ONLY`（当前时间、web_search、Workspace 只读、run_project_check、load_skill/read_skill_resource）自动执行；`MUTATING`/`EXECUTING`（Workspace 写、Skill 脚本与安装、Git、MCP）不得绕过逐次审批。
- 日常运行的模型 Key 与服务 Key 从本机私有配置读取（`data/model-config.json`、`data/service-config.json`）；评测 `--live` 与 MCP 的 env/header 按变量名从 `.env`/进程环境注入。密钥值不得进入代码、文档、日志、测试或 Git，测试禁止调用真实付费或生产服务。
- Commit 使用 `type: 中文描述`；未经用户明确要求不得提交，不得通过删除或弱化测试制造通过。
- 详细说明与架构入口：[文档首页](docs/README.md)、[Rules](docs/rules/README.md)、[Spec](docs/spec/README.md)、[Knowledge](docs/knowledge/README.md)、[编排](docs/knowledge/orchestration.md)。
