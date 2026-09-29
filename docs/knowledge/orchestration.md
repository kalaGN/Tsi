# 当前编排逻辑技术说明

本文记录截至 2026-09-28 的**已实现行为**。Tsi 目前是共享 Runtime 的单 Agent 助手：程序先进行一次无工具任务预判，模型决定是否调用已暴露的工具，程序负责确认边界、请求生命周期、会话状态和长任务验收。这不是多 Agent 分工。

## 组件与职责

| 层次 | 主要代码 | 职责 |
| --- | --- | --- |
| 入口 | `app/webui/router.py`、`app/webui/service.py`；`app/tui/application.py`、`app/tui/request.py` | 校验输入，处理流式事件、取消及交互审批；Web 还管理项目和多会话。 |
| 任务预判 | `app/runtime/task_preflight.py`、`app/tui/plan_confirmation.py`、`app/webui/approvals.py` | 每轮普通聊天和长任务先分类为直接执行、待确认计划或澄清；计划确认不授予工具权限。 |
| 会话 | `app/runtime/session.py`、`app/runtime/memory.py`、`app/runtime/session_store.py` | 为每轮请求固定执行快照，准备上下文、压缩/淘汰历史，并在成功后持久化对话。 |
| 模型与工具 | `app/runtime/chat.py`、`app/runtime/tool_loop.py`、`tools/groups.py`、`tools/policy.py` | 创建 Provider Turn，按需暴露工具组，串行执行模型步骤和白名单工具，约束调用预算。 |
| Skill/MCP | `app/runtime/skill_runtime.py`、`tools/mcp_client.py` | 在请求边界固定 Skill Catalog、提示词和 Registry；已配置的 MCP 工具进入对应的请求级 Registry。 |
| 长任务 | `app/runtime/task_runs.py`、`app/runtime/task_runner.py`、`app/runtime/task_verify.py` | 保存有界状态、执行一轮会话请求、用确定性条件验收；中断后等待人工核查。 |

## 普通对话链路

1. Web 从 `/ui/api/chat` 接收输入，由 `WebUiService.stream_message()` 建立请求 ID、事件队列和取消/审批回调；TUI 从输入框进入自己的请求处理器。两端均先调用 `ChatSession.assess_task()`，使用当前模型进行一次**无工具、有限上下文**的预判。`direct` 自动继续，`clarify` 仅展示问题；`planned` 最多展示六步，必须明确确认。预判失败、取消或未确认时不进入 `ChatSession.send()`，也不写入对话历史。
2. 已确认的计划仅作为本轮用户消息中的有限参考上下文传给 `ChatSession.send()`，不提升为系统指令；计划不扩大工具权限，后续写入/脚本/安装等仍需逐次审批。Web 计划确认等待最多 5 分钟，超时按取消处理。预判会额外产生一次模型调用及耗时；预判日志只记录类别、耗时、步数或错误码，不新增长文本日志事件。已有 Provider 传输日志仍按现行日志策略运行。
3. `ChatSession` 为本轮固定系统提示词、项目工作区、Skill Catalog 和工具 Registry；读取当前模型预算，并根据上下文占用选择原文、摘要或完整轮次淘汰。请求结束前，变动的 Skill Catalog 不改变本轮快照。
4. `run_chat_messages()` 创建所选 Provider 的 Turn；`run_tool_loop()` 反复取得模型步骤。如果步骤没有工具调用且返回有效文本，本轮结束；否则先撤销该步骤可能展示的临时文本，再执行工具并把结果交给下一模型步骤。
5. Registry 按入口白名单提供工具组。模型可通过 `activate_tool_groups` 按需看到更多工具定义，但“激活”不是授权；同一模型步骤中新激活的工具要等下一步才可调用。每个工具调用仍由 Registry 校验，需审批的写入、Skill 脚本、安装和 MCP 调用仍逐次确认。
6. 成功后会话保存用户消息与最终助手文本。Web 用有序事件流发送文本增量、工具状态、审批请求和终态；失败或取消不把未完成回答当成成功会话写入。

工作区工具循环采用最多 **41 个模型步骤、每步 4 次、总计 40 次工具调用**的上限；默认简化循环上限为 5/4/16。额外还有模型输入预算、工具载荷和请求超时边界。工具循环是**串行**的：即使一个模型步骤给出多个工具调用，也依次执行，不是并行任务图。

## 长任务链路

长任务是普通对话外的显式模式，创建时绑定会话、项目、目标与验收条件；不会因为普通聊天内容看起来复杂就自动创建。Web 调用 `run_task_step()` 包裹现有 `stream_message()`；TUI 通过 `/task new`、`/task resume` 在自身请求流程中执行，但共用 `TaskRunStore` 的状态格式与 `verify_task()` 的验收规则。两端不跨入口直接接管任务。预判或等待计划确认期间，任务仍保持 `ready`/`needs_review`，不增加 `attempts`；确认执行后才进入 `running`。

| 状态 | 含义与下一步 |
| --- | --- |
| `ready` | 已创建，等待显式运行。 |
| `running` | 一轮模型/工具请求正在执行；开始一轮才增加 `attempts`。 |
| `awaiting_approval` | 工具正在等待用户的一次性决定，获准或拒绝后的结果回到本轮请求。 |
| `verifying` | 模型请求结束，正在执行固定验收。 |
| `completed` | 所有验收条件确定性通过。 |
| `needs_review` | 验收未通过或结果不确定、超时/断流/重启中断；先人工检查工作区，再决定是否显式继续。 |
| `cancelled` / `failed` | 终态，不自动重试。 |

验收仅支持文件存在、文件不存在、文件 SHA-256 和固定项目检查；模型自述“已完成”不是验收证据。默认最多执行 3 轮；Web 单轮最多 10 分钟。重启时遗留的 `running`、`awaiting_approval`、`verifying` 会转为 `needs_review`，不会重放可能已经发生的写操作或沿用旧审批。具体使用方式见[长任务模式](task-runs.md)。

## 当前不具备的编排能力

- **没有多 Agent 角色协作**：不存在 Planner/Executor/Reviewer 等独立 Agent 的调度、交接或并行执行。
- **没有多 Agent 任务图**：预判器是一次独立模型调用，不是持久化 Planner Agent；执行仍由一个 ChatSession 串行完成，未实现跨 Agent 交接、并行子任务或自动复盘。
- **没有持久化待确认计划**：Web 使用请求级一次性审批，TUI 使用模态弹窗；断流、退出或重启后不能恢复旧确认。需要重新发起任务并重新预判。

## 修改与验证定位

- 改模型/工具往返：先检查 `app/runtime/tool_loop.py`、`tools/groups.py` 和 `tests/test_tool_loop.py`；不要只调整页面文字。
- 改上下文与历史：检查 `app/runtime/session.py`、`app/runtime/memory.py` 及对应测试；确认新设置影响**下一次真实请求**。
- 改长任务：检查 `app/runtime/task_runs.py`、`app/runtime/task_runner.py`、`app/runtime/task_verify.py` 和 `tests/test_task_runner.py`；重点验证中断后不重放副作用、验收不依赖模型自述。
- 改“先预判再确认”：检查共享预判契约、Web/TUI 的确认及取消边界，并用真实 Provider 冒烟测试额外模型往返和延迟；不能把页面进度文字当成实际预判结果。
