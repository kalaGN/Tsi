# Spec Index

本目录保存单次变更的需求文档；对应设计和任务分别保存在现有 `docs/plan/` 与 `docs/tasks/`。历史文件保留原名，不移动、不重命名。

## Active and Historical Specs

- [TUI 技能命令](20260905-TUI技能命令-需求.md)：保留 `/clear` 完整清理能力，并增加只读当前运行时 Catalog 的 `/skills` 本地命令；已完成。
- [Skill 安装与刷新](20260903-Skill安装与刷新-需求.md)：为 TUI 增加来自公开 GitHub URL 和 `~/.codex/skills` 的逐次审批安装，以及仅安装成功触发的下一请求热刷新；已完成。
- [Skill 系统](20260902-Skill系统-需求.md)：以 Codex `.agents/skills/` 为基线，为本地 TUI 增加 Catalog 渐进式披露、资源读取和逐次审批脚本执行；已完成。
- [Tsi 助手项目命名](20260901-Tsi助手项目命名-需求.md)：统一 README、TUI、Swagger/OpenAPI 和当前项目文档的正式展示名称；已完成。
- [模型日志可读格式](20260812-模型日志可读格式-需求.md)：本地滚动文件使用中文多行分块格式，HTTP stderr 保持单行 JSON，TUI 继续只写文件；已完成。
- [TUI 双击复制单行](20260812-TUI双击复制单行-需求.md)：双击对话记录中的可见渲染行时立即复制该行，保留现有拖选复制；已完成。
- [TUI 项目自修改](20260811-TUI项目自修改-需求.md)：为本地 TUI 增加受 Workspace 隔离、Diff 审批、白名单检查和安全撤销约束的项目代码修改闭环；已完成。
- [系统提示词](20260811-系统提示词-需求.md)：让 TUI 读取启动目录直属 `AGENTS.md`，并作为不持久化的 system 消息用于每轮模型请求；已完成。
- [TUI 消息复制](20260811-TUI消息复制-需求.md)：让最终消息和流式临时回答支持鼠标选择，并通过 Textual 内置快捷键复制可见文本；已完成。
- [模型流式输出](20260811-模型流式输出-需求.md)：两家上游改为流式协议，TUI 增量展示最终文本，HTTP `/chat` 保持聚合 JSON；已完成。
- [TUI 输入交互](20260810-TUI输入交互-需求.md)：增加上下键历史输入和输入框上方的请求中动画、实时耗时与取消提示；已完成。
- [TUI Markdown 渲染](20260810-TUIMarkdown渲染-需求.md)：使用现有 Rich 能力美化 TUI 中的新响应和已恢复 Assistant Markdown，其他角色保持纯文本；已完成。
- [TUI 上下文占比](20260810-TUI上下文占比-需求.md)：在底部状态栏右侧展示已提交会话历史相对可配置预算的本地估算占比；待确认。
- [工具调用编排](20260808-工具调用编排-需求.md)：为 HTTP 与 TUI 增加受白名单和循环上限约束的多模型 Function Calling 流程；已完成。
- [外部模型请求日志](20260808-外部模型请求日志-需求.md)：记录实际外部 HTTP 请求、脱敏 Header、完整 JSON Body、状态、耗时和有限失败分类；已完成。
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
