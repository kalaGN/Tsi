# Rules Index

## Documents

- [FastAPI Demo AI Coding Rules](harness-engineering-rules.md)：项目架构、HTTP 契约、编码、测试、安全、可靠性、性能、Git 与 AI 工作流的长期强制规则。适用于功能开发、缺陷修复、重构、审查和文档维护。

## Progressive Loading

1. 每次任务先读根目录 `AGENTS.md`。
2. 涉及代码或文档变更时读本索引和项目规则。
3. 再按任务加载相关 Spec、Knowledge、源文件和测试；不要一次加载全部历史文档。
4. 外部 API 任务加载 `docs/knowledge/api-conventions.md` 和相关 Service；架构任务加载 `docs/knowledge/architecture.md`；排障任务加载 `docs/knowledge/troubleshooting.md`。

## Conflict Resolution

冲突优先级：

1. 用户当前明确要求。
2. 已确认的需求 Spec。
3. 项目 Rules。
4. 项目 Knowledge。
5. 现有代码惯例。

发现冲突时必须报告冲突文件、影响和可选方案，不得静默选择。源码与 Knowledge 冲突时先以可验证源码作为当前事实，并提出文档同步；源码与已确认 Spec 冲突时不得擅自改变任一方。

## Maintenance

- 规则只记录长期、可执行、可验证的约束，不记录单次需求细节。
- 规则变化必须基于明确决策或已验证的项目变化。
- 新增、删除或重命名规则文档时同步更新本索引和 `AGENTS.md`。
- 保持 `docs/rules/` 目录层级不变；稳定规则文件不使用日期前缀。
