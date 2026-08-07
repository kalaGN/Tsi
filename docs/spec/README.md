# Spec Index

本目录保存单次变更的需求文档；对应设计和任务分别保存在现有 `docs/plan/` 与 `docs/tasks/`。历史文件保留原名，不移动、不重命名。

## Active and Historical Specs

- [当前对话上下文](20260807-当前对话上下文-需求.md)：参考 Reasonix Session 模型，为 TUI 增加可持久化和恢复的唯一多轮会话；已完成。
- [模型输入输出日志](20260807-模型调用日志-需求.md)：在关联的请求和成功响应 JSON 中记录完整明文；已完成。
- [多模型 Provider 架构](20260807-多模型Provider架构-需求.md)：重构模型接入边界并增加 DeepSeek 官方 Chat Completions Provider；已完成。
- [TUI 对话入口](20260806-TUI对话-需求.md)：参考 Reasonix 的共享运行内核思路，为当前项目增加本地全屏单轮对话 TUI；已完成。
- [阿里云 Responses API 对接](2026-07-18-aliyun-responses-api.md)：定义 `/chat` 初始契约；对应[设计](../plan/2026-07-18-aliyun-responses-api-plan.md)和[任务](../tasks/2026-07-18-aliyun-responses-api-tasks.md)。其中初始目录结构已被后续重构取代，当前结构以架构 Knowledge 为准。
- [Main 模块架构拆分](2026-07-18-main-module-refactor.md)：将实现拆为 Application、Router、Service；对应[设计](../plan/2026-07-18-main-module-refactor-plan.md)和[任务](../tasks/2026-07-18-main-module-refactor-tasks.md)。已完成。

## When a Spec Is Required

- 修改公开 API 或上游契约。
- 引入依赖、数据库、中间件、外部服务或生产基础设施。
- 修改架构、部署、安全、可靠性或性能策略。
- 涉及多个职责模块，或需要兼容、迁移、回滚方案。

## Naming for New Documents

未来新文档统一使用首次创建当天的本地日期：

```text
YYYYMMDD-简要说明-需求.md
YYYYMMDD-简要说明-设计.md
YYYYMMDD-简要说明-任务.md
```

- 同一变更的三份文档使用相同日期和简要说明。
- 简要说明控制在 2–20 个字符，不含空格、斜杠或特殊字符。
- 历史 `YYYY-MM-DD-...` 文件不重命名。
- 不为填充目录创建空 Spec。

## Required Content

- 需求：背景、目标、范围、非目标、待确认项和可测试验收标准。
- 设计：现状、方案、契约、数据、异常、安全、性能、风险和回滚。
- 任务：依赖顺序、每项验收标准、可能修改文件和真实验证命令。

需求和设计必须先经人工确认，再进入下一阶段。
