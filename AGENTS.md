# AI Development Guide

## Project

- 轻量 FastAPI HTTP 服务，对外提供根接口和阿里云 Responses API 代理接口。
- Python 3.11；依赖版本以 `requirements.txt` 为准。
- 当前不是多 Agent 系统，不含数据库、缓存、消息队列、Worker 或持久化。

## Architecture

- 启动入口：`main.py`，仅导出 `app.application.app`。
- 应用组装：`app/application.py`。
- HTTP 路由与请求校验：`app/routers/`。
- 外部服务调用与错误映射：`app/services/`。
- 测试：`tests/`。
- 真实依赖方向：`main → application → router → service`。
- 详细事实：[架构知识](docs/knowledge/architecture.md)。

## Environment and Commands

先确认当前解释器是 Python 3.11 且已安装依赖；当前机器默认 `/usr/bin/python3` 不满足要求，已验证可用解释器为 `/Users/wangfei/anaconda3/bin/python3`。

- 环境确认：`python3 --version`
- 安装：`python3 -m pip install -r requirements.txt`
- 启动：`python3 -m uvicorn main:app --reload --env-file .env`
- 测试：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q`
- 语法检查：`python3 -m compileall -q main.py app tests`
- 依赖检查：`python3 -m pip check`
- 差异检查：`git diff --check`

仓库没有配置 Formatter、Lint、类型检查、构建打包或 CI 命令，不得虚构对应门禁。

## Documentation Index

- 规则索引：[docs/rules/README.md](docs/rules/README.md)
- 项目规则：[docs/rules/harness-engineering-rules.md](docs/rules/harness-engineering-rules.md)
- 需求 Spec：[docs/spec/README.md](docs/spec/README.md)
- 设计计划：[docs/plan/README.md](docs/plan/README.md)
- 任务清单：[docs/tasks/README.md](docs/tasks/README.md)
- 知识索引：[docs/knowledge/README.md](docs/knowledge/README.md)
- 项目审计：[docs/knowledge/repository-audit.md](docs/knowledge/repository-audit.md)

## Before Changes

- 先读相关源码、测试、已确认 Spec、项目 Rules 和相关 Knowledge。
- 报告需求、文档与代码冲突，不得静默选择。
- 公开 API、外部依赖、架构、安全或性能变更必须先写 Spec 并等待确认。
- 新增或升级依赖前先说明必要性、影响和替代方案。

## After Changes

- 新功能与缺陷修复同步覆盖正常、异常和边界测试。
- 运行真实可用的语法检查、全量测试、依赖检查和 `git diff --check`。
- 同步更新受影响的 Spec、任务和 Knowledge。
- 未经用户明确要求，不创建 Git Commit。

## Hard Boundaries

- 不擅自改变 `/chat` 请求、成功响应或错误映射等公开契约。
- 密钥仅从环境变量读取；禁止进入代码、文档、日志、测试或 Git。
- 不无需求增加层级、服务、基础设施或第三方依赖。
- 自动化测试禁止调用真实付费或生产外部服务。
- 不删除或弱化测试来制造通过结果。
- 数据库、部署、认证授权、重试、性能等未落地能力不得描述为已实现。
- 文档命名与维护规则以规则索引为准；保持现有 `docs/` 目录结构不变。
