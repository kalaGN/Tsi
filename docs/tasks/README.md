# Task Index

本目录保存已确认设计对应的可执行任务清单，保持现有目录层级不变。

## Documents

- [TUI 输入交互任务](20260810-TUI输入交互-任务.md)：测试优先实现活动 Timer、上下键历史、草稿恢复和文档同步；已完成。
- [TUI Markdown 渲染任务](20260810-TUIMarkdown渲染-任务.md)：测试优先实现 Assistant Markdown 展示、纯文本边界和文档同步；已完成。
- [工具调用编排任务](20260808-工具调用编排-任务.md)：实现根目录工具 Registry、当前时间工具、多模型协议续接、有界 Runtime 循环和入口回归；已完成。
- [外部模型请求日志任务](20260808-外部模型请求日志-任务.md)：扩展日志契约，在真实 HTTP 边界记录完整 `messages/input`、状态、耗时和安全失败分类；已完成。
- [当前对话上下文任务](20260807-当前对话上下文-任务.md)：实现消息契约、本地持久化、恢复、TUI 接入与回归测试；已完成。
- [模型输入输出日志任务](20260807-模型调用日志-任务.md)：扩展日志契约、Runtime 关联与明文风险回归；已完成。
- [多模型 Provider 架构任务](20260807-多模型Provider架构-任务.md)：统一模型接入架构并增加 DeepSeek Provider；对应[需求](../spec/20260807-多模型Provider架构-需求.md)和[设计](../plan/20260807-多模型Provider架构-设计.md)，已完成。
- [TUI 对话入口任务](20260806-TUI对话-任务.md)：已完成；对应[需求](../spec/20260806-TUI对话-需求.md)和[设计](../plan/20260806-TUI对话-设计.md)。
- [阿里云 Responses API 对接任务](2026-07-18-aliyun-responses-api-tasks.md)：已完成；对应[需求](../spec/2026-07-18-aliyun-responses-api.md)和[设计](../plan/2026-07-18-aliyun-responses-api-plan.md)。
- [Main 模块架构拆分任务](2026-07-18-main-module-refactor-tasks.md)：已完成；对应[需求](../spec/2026-07-18-main-module-refactor.md)和[设计](../plan/2026-07-18-main-module-refactor-plan.md)。

## Maintenance

- 未来文件按 `YYYYMMDD-简要说明-任务.md` 命名；与需求、设计使用相同日期和简要说明。
- 每项任务包含依赖顺序、验收标准、可能修改文件和项目真实可执行的验证命令。
- 每次只推进一个可验证任务，完成后更新复选框；不得用删除测试或吞掉错误制造完成状态。
