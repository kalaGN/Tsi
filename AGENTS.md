# AI Development Guide

## Project

- FastAPI Demo，使用 Python 3.11、FastAPI、Pydantic、HTTPX 和 Pytest。
- 应用入口：`main.py`。
- 自动化测试：`tests/`。
- 依赖声明：`requirements.txt`。

## Architecture

- 当前项目规模较小，路由、请求模型和上游调用集中在 `main.py`。
- 只有在职责或规模明显增长时才拆分模块，避免提前分层。
- 详细开发规则：[Harness 开发规范](docs/rules/harness-engineering-rules.md)。

## Commands

- 安装：`python3 -m pip install -r requirements.txt`
- 启动：`python3 -m uvicorn main:app --reload --env-file .env`
- 测试：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q`
- 语法检查：`python3 -m compileall -q main.py tests`

## Documentation Index

- 规格：`docs/spec/`
- 实施计划：`docs/plan/`
- 任务清单：`docs/tasks/`
- 项目规则：`docs/rules/`
- 项目知识库：`docs/knowledge/`

## Always Apply

- 中大型、跨文件或需求不清晰的变更必须先写 Spec，经人工确认后再编码。
- 新功能和 Bug 修复必须有覆盖正常与异常路径的自动化测试。
- 交付前必须完成语法检查、测试、规则合规检查和架构审查。
- 密钥只从环境变量读取，禁止进入代码、文档、日志和 Git。
- 一次提交只处理一个主题，使用清晰的 Conventional Commit 信息。
- 新生成文档使用 `YYYY-MM-DD-<document-name>.<extension>` 文件名；`docs/rules/` 和 `docs/knowledge/` 下的稳定文档除外。
