# Design Index

本目录保存已确认需求对应的设计和实施计划，保持现有目录层级不变。

## Documents

- [阿里云 Responses API 对接设计](2026-07-18-aliyun-responses-api-plan.md)：初始模型接口接入设计；对应[需求](../spec/2026-07-18-aliyun-responses-api.md)和[任务](../tasks/2026-07-18-aliyun-responses-api-tasks.md)。
- [Main 模块架构拆分设计](2026-07-18-main-module-refactor-plan.md)：Application、Router、Service 拆分设计；对应[需求](../spec/2026-07-18-main-module-refactor.md)和[任务](../tasks/2026-07-18-main-module-refactor-tasks.md)。

## Maintenance

- 新设计必须基于已确认需求，记录当前事实、依赖方向、异常、安全、性能、风险、回滚和验证点。
- 未来文件按 `YYYYMMDD-简要说明-设计.md` 命名；历史文件不重命名。
- 设计变化先更新文档并重新确认，不得先改代码后补决策。
