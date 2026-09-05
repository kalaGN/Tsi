# Tsi 助手架构

## 概览

Tsi 助手是一个基于 Python 3.11 的轻量模型调用项目，同时提供聚合 JSON 的无状态 FastAPI HTTP 和支持流式展示、可恢复单会话及项目自修改的 Textual TUI。两个入口共享 Chat Runtime，但使用隔离的 Registry 和循环预算：HTTP 仅有只读时间工具，TUI 绑定启动目录并提供读取、审批写入、固定检查和撤销工具。

## 组件

- `main.py`：从 `app.application` 导出 FastAPI 应用。
- `app/application.py`：创建 FastAPI、注册根路由和 Chat Router。
- `app/observability/model_logging.py`：配置模型/HTTP/工具 JSON 日志、可选终端输出、本地转储和事件白名单。
- `app/routers/chat.py`：校验 `POST /chat`，返回统一 `ChatResponse` 并映射 HTTP 错误。
- `app/runtime/chat.py`：无状态入口、有序消息调用、默认工具 Registry、统一结果和安全错误语义。
- `app/runtime/tool_loop.py`：默认/Workspace 循环预算、请求级审批上下文、串行工具编排和结果观察回调。
- `app/runtime/session.py`：串行化 TUI 发送，只提交 Provider 和持久化均成功的完整轮次。
- `app/runtime/skill_runtime.py`：持有当前 Skill Catalog、共享 Workspace Journal 和安装器，在每次发送开始时生成不可变执行快照。
- `app/runtime/session_store.py`：版本化 JSON 会话校验、原子保存、恢复与清理。
- `app/services/llm/contracts.py`：中立角色/消息、ModelStep、Provider Turn、文本 Delta/reset 回调协议和共享异常。
- `app/services/llm/factory.py`：解析环境并创建当前 Provider。
- `app/services/llm/http_client.py`：共享异步 SSE POST、有界事件解码、流生命周期超时、I/O 边界事件记录和脱敏错误处理。
- `app/services/llm/aliyun.py`：阿里云 Responses 流事件累加、Function Call 续接和完整结果校验。
- `app/services/llm/deepseek.py`：DeepSeek Chat Completions 流事件累加、assistant/tool 续接和完整结果校验。
- `tools/contracts.py`：Provider 中立的风险等级、Tool、审批请求、调用、结果和回调契约。
- `tools/registry.py`：显式白名单注册、参数解析、两阶段审批、串行执行、安全错误和载荷边界。
- `tools/builtin.py`：只读 `get_current_time(timezone)` 实现。
- `tools/workspace.py`：Workspace 路径策略、文件/Git 工具、结构化编辑、Journal 和撤销。
- `tools/project_checks.py`：无 Shell 的四个固定项目检查。
- `tools/skills.py`：安全 YAML Skill Catalog、不可变资源快照、渐进读取工具及需审批的有界脚本执行器。
- `tools/skill_installation.py`：公开 GitHub/个人 Codex 来源解析、无跟随复制、安装审批、候选校验、原子提交和刷新回滚。
- `app/runtime/system_prompt.py`：从 TUI 启动目录有界读取可选 `AGENTS.md`，并与 Skill Catalog 组合为单条系统提示词。
- `app/tui/__main__.py`：加载根目录 `.env`，捕获一次启动目录，独立加载 Skill，创建 TUI Registry 并启动 Textual。
- `app/tui/application.py`：终端输入与历史、消息展示、请求活动、审批回调、已落盘失败提醒、状态、耗时和取消。
- `app/tui/approval.py`：默认拒绝且可复制的纯文本完整 Diff Modal。
- `app/tui/command_palette.py`：封装命令候选定义、前缀过滤、循环选择、补全消费与关闭状态；不执行命令，不调用 Runtime。
- `app/tui/transcript.py`：`Transcript` 渲染用户卡片、Assistant Markdown 和系统纯文本；`StreamOutput` 管理流式纯文本缓冲、批量绘制与清理。主应用仍拥有请求代次与取消校验。
- `app/tui/styles/application.tcss`、`approval.tcss`：分别由 App 和审批 Screen 的 `CSS_PATH` 加载，维护布局与外观；Rich 消息卡片样式仍由消息渲染代码负责。
- `app/tui/widgets.py`：为 RichLog 补齐鼠标选择坐标、选择高亮、可见文本复制及可选的双击单行复制，并为输入框补充 `Cmd+A` / `Ctrl+A` 全选。
- `app/tui/state.py`：定义 `Ready`、`Thinking`、`Awaiting approval`、`Error`。
- `tests/test_llm_providers.py`：Provider、工厂和共享 HTTP Mock 测试。
- `tests/test_chat_runtime.py`：Runtime 单元测试。
- `tests/test_tool_loop.py`、`tests/test_tools.py`：有界编排、审批、Registry 和内置工具测试。
- `tests/test_workspace_tools.py`：路径、文件、Git、编辑、检查和撤销测试。
- `tests/test_skills.py`：Skill 发现、快照、渐进读取、审批执行及进程清理测试。
- `tests/test_model_logging.py`：日志格式、幂等、转储和失败降级测试。
- `tests/test_chat.py`：HTTP 契约与 Provider 接线测试。
- `tests/test_tui.py`：Textual 无头交互测试。

## 依赖方向

```text
main.py -> app.application -> app.routers.chat --------+
                    |                                  |
                    +-> configure model logging        |
                                                       v
                                               app.runtime.chat
                                              /          |          \
                                             v           v           v
                              app.observability   tool_loop    provider factory
                                                      |          /          \
                                                      v         v            v
                                                  root tools  AliyunTurn  DeepSeekTurn
                                                                 \          /
                                                                  shared HTTP

python -m app.tui -> AGENTS + SkillRuntime -> app.tui.application
                              |       |
                              |       +-> install_skill -> next Catalog version
                              v
                    request snapshot -> ChatSession -> app.runtime.chat
```

- Router 和 TUI 只依赖 Runtime，不理解外部响应结构。
- Runtime 只依赖 Provider 契约、工厂和根目录工具契约，不导入具体 Provider 模块。
- Tool Loop 只理解 ModelStep、ToolCall、ToolResult 和 Registry，不理解两家上游 JSON。
- 根目录 `tools/` 不依赖 Runtime、Router、TUI 或具体 Provider。
- 工厂只解析配置和创建 Provider，不编排用例。
- Provider 为每个用户请求创建短生命周期 Turn，持有私有续接消息，构造请求并提取中立步骤；共享 HTTP 层处理网络和通用状态错误。
- Provider 层不依赖 Runtime、Router、TUI 或 Application。
- HTTP/TUI 启动入口幂等配置日志；Runtime 记录一次 `llm_request/llm_response`，每个模型步骤记录 HTTP 边界事件，每次本地执行记录 `llm_tool_call/llm_tool_result`，全链路共用同一 request ID。

## HTTP 对话流程

```text
POST /chat
  -> ChatRequest validates strict nonblank input
  -> Runtime creates the environment-selected Provider and a request ID
  -> Runtime writes llm_request with complete input_text
  -> Runtime creates the default read-only Registry and Provider Turn
  -> bounded loop calls Turn.next()
     -> Provider builds protocol-specific stream payload and calls shared SSE HTTPX
     -> Provider accumulates bounded text/tool events; HTTP supplies no display callback
     -> ModelStep contains final text or ToolCall list
     -> if tools: Registry validates and executes them serially
     -> Runtime passes ordered ToolResult list back to the same Turn
  -> loop stops when Provider returns final output_text
  -> Runtime writes llm_response with complete output_text
  -> Router returns 200 {"output_text": "..."}
```

不需要工具时仍只有四个成功事件。需要工具时，同一 request ID 下会出现多组 HTTP 事件和工具事件；`llm_tool_call` 明文记录完整 JSON 参数，`llm_tool_result` 明文记录完整安全结果。共享 HTTP 层在真实 I/O 边界旁路记录，不修改状态码映射、重试或超时；`llm_http_request.request_body` 与实际 payload 一致，因此续接请求也会明文包含工具结果。

DeepSeek Turn 按 choice/tool index 拼接流式文本和工具参数，把 assistant `tool_calls` 和对应 `role=tool/tool_call_id` 结果加入 messages，并保留工具调用时的 `reasoning_content`。阿里云 Turn 消费 `output_text.delta/done`、function call 与 `response.completed`，把每个 `function_call` 与对应 `function_call_output` 紧邻加入 input。两家结构都在 Provider 内转换为中立 ToolCall/ToolResult。

DeepSeek 必须收到合法终止原因和 `[DONE]`；阿里云必须收到成功的 `response.completed`。事件 JSON、UTF-8、终止标记、完成文本或工具结构不一致均属于无效上游响应并映射为 502。单 SSE 事件上限 96 KiB，单步文本上限 1 MiB，流解析层单工具参数上限 64 KiB；Registry 再按具体工具执行 8 KiB 或 64 KiB 上限。

## TUI 对话流程

```text
python -m app.tui
  -> load .env without overriding Shell variables
  -> read cwd/AGENTS.md once as an optional bounded system prompt
  -> load cwd/.agents/skills as initial Catalog
  -> capture cwd once and create Workspace Policy, shared Journal and SkillRuntime
  -> Runtime resolves Provider, model and safe key status
  -> load data/chat-session.json and restore complete turns
  -> Textual Worker calls ChatSession.send
  -> send reads one system prompt/Registry/Catalog execution snapshot
  -> Runtime sends optional system + committed history + current user and runs tool loop
  -> read tools execute automatically; mutating tools preview a full bounded Diff
  -> Skill install previews source/target/network risk; scripts preview command/no-sandbox risk
  -> Textual Modal defaults to reject and returns a request-scoped decision
  -> approved edit is revalidated, atomically applied and recorded in the Journal
  -> fixed checks run without Shell; undo uses the latest unchanged change_id
  -> Provider text Delta crosses the neutral callback boundary
  -> request-scoped 100 ms Timer batches temporary plain-text output, spinner and elapsed time
  -> tool step reset removes text that is not the final answer
  -> persist the new complete turn atomically
  -> TUI removes temporary output and renders the complete Assistant content as Rich Markdown
  -> TUI stops the Timer, clears activity and records final monotonic elapsed time
```

TUI 不解析 Provider JSON，也不逐次确认只读工具。启动入口只读取 `Path.cwd()/AGENTS.md` 一次，并把同一启动目录固定为 Workspace；AGENTS 不热加载。SkillRuntime 启动时加载 Catalog，之后只在一次获批安装完整成功时发布下一版本，不监控手动目录变化。`ChatSession.send()` 开始时只取一次 system prompt、Registry 和 Catalog 快照，因此当前 Provider Turn 不会使用刚安装的 Skill；下一次发送才生效。系统提示词、Skill 内容和工具轨迹不进入 Session，Session 仍只提交最终 user/assistant。文件审批 Modal 显示相对路径和完整 Diff；安装审批显示安全来源、固定目标和联网风险；脚本审批显示 Skill、相对脚本、转义命令和无沙箱风险。安装只允许公开 GitHub Contents API 或当前用户 Codex 直属目录，候选经项目临时目录校验和原子 rename，刷新失败回滚。脚本每次都重新审批，使用固定解释器、最小环境、30 秒超时和 32 KiB 合计输出边界，并在超时、输出超限或取消时终止进程组。成功编辑保留 `change_id`，Journal 最多 10 个批次且不跨重启；Registry 快照复用同一 Journal。请求代次会阻止取消后的陈旧 Delta 或审批结果写回。HTTP `/chat` 不加载 Workspace/Skill 安装模块、不读取 Home、宿主规则或 TUI 会话文件，仍在 Runtime 汇总完成后返回 JSON。

输入历史是 `ChatTuiApp` 内存状态：启动时从 Session 的 user 消息初始化，当前进程每次真正启动的请求立即追加，因此失败或取消输入也可临时召回；只有完整成功轮次由既有 Session 规则跨重启保存。高优先级 Up/Down Binding 负责不循环浏览和草稿恢复，不修改 Session schema。

## 配置

| Provider | Selector | Key | Optional model | Default |
| --- | --- | --- | --- | --- |
| Aliyun | `LLM_PROVIDER=aliyun` | `DASHSCOPE_API_KEY` | `ALIYUN_MODEL` | `qwen3-max` |
| DeepSeek | `LLM_PROVIDER=deepseek` 或未设置 | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL` | `deepseek-v4-flash` |

显式空白或未知 `LLM_PROVIDER` 是配置错误，不静默回退。模型变量空白时使用默认值。上游 URL 固定在相应适配器中，不能通过环境变量覆盖。

## 设计决策

- HTTP 与 TUI 都只接触统一文本，原始 Provider JSON 只存在于 Provider 调用栈。
- 所有请求统一通过 Provider Turn，不保留旧 `generate()` 或原始 ProviderResult 路径。
- HTTP 默认 Registry 仅注册 `get_current_time(timezone)`；TUI Registry 另注册 8 个 Workspace 工具和始终可用的 `install_skill`，并在 Catalog 非空时追加 `load_skill`、`read_skill_resource`、`run_skill_script`。工具名只能来自显式白名单，不支持反射、动态 import、任意命令或 MCP。
- HTTP 循环最多 5 步、每步 4 次、总计 16 次；TUI 最多 20 步、每步 4 次、总计 40 次。普通参数/结果上限为 8/32 KiB，编辑参数为 64 KiB。
- 写 Tool 必须先生成完整有界 Diff；Registry 没有审批回调、用户拒绝或内容并发变化时均不会执行。
- Workspace 拒绝越界、符号链接、保护路径、二进制和超限文件；编辑只支持 create/replace，固定检查不接受额外 argv、cwd 或环境。
- 第 5 步仍请求工具时不执行无法被后续步骤消费的调用，Runtime 返回安全 `tool_limit`，HTTP 映射为 502。
- `/chat` 请求不包含 Provider 或模型；切换由部署环境控制。
- 使用现有异步 HTTPX，不引入 Provider SDK。
- 保持连接 10 秒、从请求开始到流消费结束总计 60 秒超时；不实现自动重试或故障转移。
- SSE 按字节切分边界并严格解码 UTF-8；取消沿调用栈传播并由 HTTPX 上下文关闭响应流。
- 每次调用创建并关闭 HTTP Client；当前没有性能基线，不增加应用级连接生命周期。
- 上游错误体、Authorization、密钥和内部堆栈不进入 HTTP/TUI。
- 模型事件由固定字段白名单定义；HTTP stderr 渲染为单行 JSON，本地文件渲染为北京时间中文分块。Runtime、所有模型步骤和工具事件共用同一 request ID，工具事件另用上游 call ID 关联。
- Runtime 生成 request ID 并显式经 Provider 传给共享 HTTP 层，不使用 ContextVar 或全局当前 ID。
- 输入、输出和完整请求体以明文进入本地文件，HTTP 入口还会写入 stderr，多轮历史在每次调用时重复落盘；仍不记录环境 API Key、真实 `Authorization`、Provider 原始响应体、Cookie 或异常原文。
- TUI 系统提示词随完整 Provider 请求体明文进入模型日志；状态栏和 Runtime 摘要日志不回显正文。
- HTTP 边界脱敏 Header 由日志层用固定值重建（`Authorization` 写为 `Bearer [REDACTED]`），从数据流上阻止密钥进入 Logger。
- HTTP 耗时用 `time.monotonic()` 计算并保留两位毫秒；超时/连接失败只记录有限分类和耗时，不记录异常类名或堆栈。
- HTTP 日志双写单行 JSON stderr 和 UTF-8 中文分块 `logs/model-calls.log`，文件不可用时降级为 stderr；TUI 只写该文件，文件不可用时静默放弃日志，避免任何日志覆盖全屏终端。文件内容使用北京时间，结构化正文以两空格 JSON 缩进展示；单文件 10 MiB，保留 5 个备份。
- TUI 同时最多一个请求；Esc 优先清空非空输入且不启动退出计时，输入为空时第一次 Esc 取消请求，1.5 秒内第二次 Esc 退出，并用请求代次阻止陈旧结果写回。
- TUI 每个活动请求最多创建一个 100 ms Timer，空闲时没有周期任务；Timer 回调同样校验捕获的请求代次。
- TUI 上下键固定用于输入历史，历史不去重、不循环且没有独立持久化文件；`/clear` 同步清空。
- TUI 使用唯一 `data/chat-session.json` 保存完整轮次；启动恢复，`/clear` 删除，损坏历史不自动覆盖。
- TUI 只在启动时读取当前目录直属 `AGENTS.md`，不递归、不热重载；system 消息与 Session schema 隔离。
- TUI 从 `.agents/skills/*/SKILL.md` 读取 Codex 兼容项目 Skill；Catalog 仅含名称、描述和相对位置，正文与资源按需读取，任一非法 Skill 会禁用整批但不影响 Workspace 和安装工具。
- `install_skill` 每次审批，只支持匿名公开 GitHub 规范目录 URL 和当前用户 Codex Skill 直属目录；候选有界校验、同名拒绝、原子提交、刷新失败回滚，成功结果从下一次请求生效。不存在覆盖、升级、卸载、自动同步或文件监控。
- Skill 脚本仅支持快照内 `.py`/`.sh`，每次审批，不使用 `shell=True`，不继承宿主密钥环境；当前无文件系统或网络沙箱，该风险必须由审批界面和文档明确展示。
- TUI 只对 Assistant 原文做 Rich Markdown 展示，不执行代码、加载远程内容或改变 Session/HTTP 文本契约。
- TUI 临时流只展示当前请求的纯文本；成功、错误、取消、工具 reset 和退出都会清理，部分文本不持久化。
- TUI transcript 与临时流支持选择和复制当前可见文本；仅 transcript 启用双击复制命中的当前渲染行，并以内容坐标加纵向滚动偏移定位。复制通道使用 Textual 内置剪贴板与终端 OSC 52，不调用系统命令。
- TUI 输入框以局部 TextArea 子类提供 `Cmd+A` / `Ctrl+A` 全选，兼容关闭 Kitty 扩展键盘协议后的终端按键降级。
- 会话使用标准库 UTF-8 JSON 和同目录原子替换，不引入数据库或新依赖；历史明文且没有长度裁剪。
- 当前不增加 Repository、Manager、数据库、缓存或其他无实际职责的层级。
