# Design Index

本目录保存已确认需求对应的设计和实施计划，保持现有目录层级不变。

## Documents

- [当前对话上下文设计](20260807-当前对话上下文-设计.md)：定义唯一本地会话、原子 JSON 持久化、恢复和 Provider 多轮消息契约；已完成。
- [模型输入输出日志设计](20260807-模型调用日志-设计.md)：使用同一 request ID 记录完整输入和成功输出，不改 HTTP 和 Provider 契约；已完成。
- [多模型 Provider 架构设计](20260807-多模型Provider架构-设计.md)：定义统一 Provider 契约、阿里云与 DeepSeek 适配、HTTP 文本响应及迁移测试方案；对应[需求](../spec/20260807-多模型Provider架构-需求.md)和[任务](../tasks/20260807-多模型Provider架构-任务.md)，已完成。
- [TUI 对话入口设计](20260806-TUI对话-设计.md)：定义共享 Chat Runtime、Textual 界面、异步取消、错误映射和测试方案；对应[需求](../spec/20260806-TUI对话-需求.md)和[任务](../tasks/20260806-TUI对话-任务.md)，已完成。
- [阿里云 Responses API 对接设计](2026-07-18-aliyun-responses-api-plan.md)：初始模型接口接入设计；对应[需求](../spec/2026-07-18-aliyun-responses-api.md)和[任务](../tasks/2026-07-18-aliyun-responses-api-tasks.md)。
- [Main 模块架构拆分设计](2026-07-18-main-module-refactor-plan.md)：Application、Router、Service 拆分设计；对应[需求](../spec/2026-07-18-main-module-refactor.md)和[任务](../tasks/2026-07-18-main-module-refactor-tasks.md)。

## Maintenance

- 新设计必须基于已确认需求，记录当前事实、依赖方向、异常、安全、性能、风险、回滚和验证点。
- 未来文件按 `YYYYMMDD-简要说明-设计.md` 命名；历史文件不重命名。
- 设计变化先更新文档并重新确认，不得先改代码后补决策。
