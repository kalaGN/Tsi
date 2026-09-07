# Tsi 助手开发指南

- Python 3.11 项目，提供共享 Runtime 的 FastAPI HTTP 与 Textual TUI；当前不是多 Agent 系统。
- HTTP 入口：`main.py → app/application.py → app/routers/ → app/runtime/`。
- TUI 入口：`python -m app.tui → app/tui/ → app/runtime/`；模型适配在 `app/services/llm/`，工具在 `tools/`。
- 安装：`.venv/bin/python -m pip install -r requirements.txt`；启动：`.venv/bin/python -m app.tui` 或 `.venv/bin/python -m uvicorn main:app --reload --env-file .env`。
- 验证：`.venv/bin/python -m compileall -q main.py app tools tests`；`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q`；`.venv/bin/python -m pip check`；`git diff --check`。
- 修改前按需读取相关源码、测试、已确认 Spec 和项目文档；发现冲突必须报告，不得静默选择。
- 显著的公开 API、依赖、架构、安全或性能变更必须先写 Spec 并等待确认。
- 必要注释只解释非显而易见的意图与边界；项目文档使用中文，新功能和修复必须补测试并同步文档。
- `/chat` 成功响应固定为 `{"output_text":"..."}`；Router 和 TUI 不得暴露 Provider 原始响应。
- 工具仅从 `tools/` 显式注册；HTTP 只自动执行无副作用工具，TUI 写操作和 Skill 脚本不得绕过审批。
- 密钥只读环境变量且不得进入代码、文档、日志、测试或 Git；测试禁止调用真实付费或生产服务。
- Commit 使用 `type: 中文描述`；未经用户明确要求不得提交，不得通过删除或弱化测试制造通过。
- 详细规则与架构入口：[Rules](docs/rules/README.md)、[Spec](docs/spec/README.md)、[Knowledge](docs/knowledge/README.md)。
