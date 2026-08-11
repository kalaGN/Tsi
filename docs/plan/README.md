# Design Index

本目录保存已确认需求对应的设计和实施计划，保持现有目录层级不变。

## Documents

- [系统提示词设计](20260811-系统提示词-设计.md)：定义启动目录 AGENTS 读取、system 消息契约、Session 隔离和 TUI 阻断状态；已完成。
- [TUI 消息复制设计](20260811-TUI消息复制-设计.md)：以项目内 SelectableRichLog 补齐选择坐标、高亮和可见文本提取，复用 Textual 内置复制动作；已完成。
- [模型流式输出设计](20260811-模型流式输出-设计.md)：定义共享 SSE 边界、两家 Provider 解析、Runtime 回调、工具步骤撤销和 TUI 临时输出；已完成。
- [TUI 输入交互设计](20260810-TUI输入交互-设计.md)：使用请求专属 Timer 实现输入框上方活动提示，并以高优先级 Up/Down 管理输入历史；已完成。
- [TUI Markdown 渲染设计](20260810-TUIMarkdown渲染-设计.md)：仅在 Textual 展示层将 Assistant 原文构造为 Rich Markdown Renderable；已完成。
- [工具调用编排设计](20260808-工具调用编排-设计.md)：根目录工具 Registry、Provider Turn、多模型协议续接、有界循环和只读时间工具；已完成。
- [外部模型请求日志设计](20260808-外部模型请求日志-设计.md)：在共享 HTTP 边界记录完整 `messages/input`、脱敏 Header、状态、耗时和失败分类；已完成。
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
