# 工具与扩展

内置工具清单、按需工具组、MCP 接入及审批边界。Skill 目录结构、引用和安装示例见[使用指南](usage.md#启动-tui)。

[返回文档首页](../README.md) · [项目首页](../../README.md)

## 工具调用

### 外部 MCP 工具

在项目根目录创建 Git 忽略的 `data/mcp-servers.json`，列出要启用的 MCP Server。安装依赖后，Web UI 和 TUI 都会在每轮对话开始时连接、发现工具，并按需提供 `mcp` 工具组；每次调用都需要本地审批。未创建配置文件时不会连接外部服务。

```json
{
  "servers": [
    {"name": "local", "transport": "stdio", "command": "/path/to/server", "args": ["--stdio"], "env": {"SERVICE_TOKEN": "MY_SERVICE_TOKEN"}},
    {"name": "remote", "transport": "streamable_http", "url": "https://example.com/mcp", "headers": {"Authorization": "MY_MCP_AUTH_HEADER"}}
  ]
}
```

`env` 和 `headers` 的值是宿主环境变量名；例如先在 `.env` 中配置 `MY_SERVICE_TOKEN` 或 `MY_MCP_AUTH_HEADER`。HTTP Authorization 变量值应包含完整的 `Bearer ...`。本机 HTTP 仅允许 `localhost`、`127.0.0.1` 或 `::1`，远程服务必须使用 HTTPS。stdio 服务器是本机进程，会在本轮对话开始时启动；仅配置可信命令。连接与工具响应在请求结束或取消时清理。首版仅提供文本和结构化工具结果，不支持 MCP Resources、Prompts、OAuth 或管理界面。外部内容可能进入模型上下文；启用 MCP 的请求在本机模型日志及评测轨迹中隐藏正文。

Web UI 与 TUI 使用不同的显式工具白名单。Web UI 可按需激活时间、网络搜索、Workspace 读写和可选 Skill 工具，但不包含 Git 写工具；TUI 每轮首步只发送 `activate_tool_groups`，由模型按任务意图激活所需工具组，避免每个模型步骤重复携带完整工具 Schema。一次请求最多追加两次，可在一次激活中选择多个组；组状态不会跨请求保留。显式 `$技能名` 会预激活 `skills`，不消耗追加次数。创建、修改、撤销、安装或执行脚本的审批规则不因激活而改变。

| 工具组 | 包含能力 |
|---|---|
| `general` | 当前时间 |
| `web_search` | 固定 Serper.dev 上游的公开网络搜索 |
| `workspace_read` | 文件列举、搜索、读取及 Git 状态、Diff |
| `workspace_write` | `workspace_read` 全部能力，加结构化修改、单文件删除、固定检查和撤销 |
| `skills` | 加载 Skill、读取资源、执行脚本 |
| `skill_install` | 安装 Skill |
| `git_write` | 暂存指定文件、创建中文提交、推送既有上游 |
| `mcp` | 当前请求发现的外部 MCP 工具，逐次本地审批 |

`skills` 只在启动 Catalog 非空时可选；`skill_install` 只在当前入口安装器可用时可选。模型不能创建新组或把任意工具加入组。

| 使用入口 | 工具 | 作用 | 执行方式 |
| --- | --- | --- | --- |
| Web UI、TUI | `get_current_time(timezone)` | 获取指定 IANA 时区（例如 `Asia/Shanghai`）的当前 ISO 8601 时间 | 自动执行 |
| Web UI | `web_search(query, limit)` | 搜索公开网络并返回有界标题、HTTP(S) 链接和摘要 | 自动执行；需在「设置 → 服务」保存 Serper Key |
| Web UI、TUI | `list_workspace_files` | 分页列举允许读取的文件和目录 | 自动执行 |
| Web UI、TUI | `search_workspace_text` | 一次扫描搜索 1～4 个字面量关键词，返回命中的文件和行号 | 自动执行 |
| Web UI、TUI | `read_workspace_files` | 一次读取最多 4 个独立文本片段及各文件 SHA-256，减少模型往返 | 自动执行 |
| Web UI、TUI | `read_workspace_file` | 按行读取单个文件并返回 SHA-256 | 自动执行 |
| Web UI、TUI | `get_workspace_git_status` | 查看 Git 状态 | 自动执行 |
| Web UI、TUI | `get_workspace_git_diff` | 查看分页 Diff | 自动执行 |
| Web UI、TUI | `apply_workspace_edits` | 创建文件或执行带哈希前置条件的精确替换 | 本地审批后执行 |
| Web UI、TUI | `delete_workspace_file` | 删除一个带哈希前置条件的 UTF-8 文本文件 | 本地审批后执行，可在当前请求撤销 |
| Web UI、TUI | `run_project_check` | 运行 `compile`、`test_all`、`pip_check`、`diff_check` 四个固定检查 | 自动执行 |
| Web UI、TUI | `undo_workspace_change` | 撤销当前请求最近一次 Agent 修改 | 本地审批后执行 |
| Web UI、TUI | `install_skill` | 从公开 GitHub Skill 目录或当前用户 `~/.codex/skills` 直属目录安装 Skill | 每次本地审批后安装，下一次请求生效 |
| Web UI、TUI | `load_skill` | 按名称读取完整 `SKILL.md` 和资源清单 | 自动执行 |
| Web UI、TUI | `read_skill_resource` | 读取 Skill 快照中的 UTF-8 文本资源 | 自动执行 |
| Web UI、TUI | `run_skill_script` | 执行 Skill `scripts/` 中的 `.py` 或 `.sh` 文件 | 每次本地审批后执行 |
| 仅 TUI | `git_stage` | 暂存 1 至 20 个明确指定的普通文件 | 每次本地审批后执行 |
| 仅 TUI | `git_commit` | 以 `type: 中文描述` 提交当前暂存内容 | 每次本地审批后执行 |
| 仅 TUI | `git_push` | 非强制推送当前分支到既有上游 | 每次本地审批后执行并访问网络 |

工作区搜索在已知多个相关关键词时可传 `query` 加最多 3 个 `additional_queries`，只扫描一次。读取默认最多返回 400 行；需要定位片段时可显式传 `start_line`、`max_lines`。多个目标文件优先使用 `read_workspace_files`。

典型流程为：模型先列举、搜索、读取和检查现有差异，再提出结构化修改或单文件删除；Web UI/TUI 显示相对路径和完整有界 Diff，默认焦点为拒绝。确认后模型可运行检查并继续修正。每个写入、删除和撤销都独立审批；Web Journal 只存在单次请求，TUI Journal 最多保存 10 个批次且只存在当前进程，重启后不能撤销旧批次。

安全和成本边界：

- 内置工具只能从根目录 `tools/` 显式注册；外部 MCP 工具只来自本地明确配置的 Server，调用逐次审批。不提供模型自由拼接的 Shell/Python、动态 import、数据库或依赖安装。Git 只能通过三个固定结构化工具执行，不能传入任意命令或参数。
- `web_search` 只向固定的 `https://google.serper.dev/search` 发送查询，模型不能指定 URL、Header、请求方法或搜索供应商；查询内容会发送给 Serper.dev，响应体限制为 1 MiB。
- Workspace 为 Web 当前项目配置的目录，或 TUI 的启动目录；绝对路径、`..`、符号链接、二进制和保护路径会被拒绝。
- `.env*`、`.git/`、`.venv/`、`data/`、`logs/` 和缓存目录不可读写；`AGENTS.md`、Rules、依赖文件和 Workspace 安全实现额外禁止写入。
- `apply_workspace_edits` 只支持创建已有目录下的 UTF-8 文件和精确替换，不支持删除、移动、重命名或创建目录。
- `delete_workspace_file` 只接受一个现有 UTF-8 普通文本文件及 `read_workspace_file` 返回的当前 SHA-256；拒绝目录、链接、批量、保护路径、审批后变化和无审批删除，成功后可用 `change_id` 撤销。
- Runtime 默认最多 5 个模型步骤、每步 4 次、总计 16 次工具调用；Web UI/TUI 分别为 41、4、40，工具组激活也计入预算。
- 普通参数最多 8 KiB，编辑参数最多 64 KiB，结果最多 32 KiB；多个调用串行执行。
- 同一用户请求内的模型步骤复用一个短生命周期 HTTP 连接池；请求成功、失败或取消后关闭。正式日志分别记录响应头、首个 SSE 事件、首个可展示文本和完整流耗时，未发生的阶段显示 `-`。
- Skill 脚本使用参数数组而非 Shell 拼接，`.py` 固定使用当前 Python，`.sh` 固定使用 `/bin/sh`；运行环境不继承 API Key 等宿主变量，30 秒超时，stdout/stderr 合计最多 32 KiB。
- Skill 脚本没有文件系统或网络沙箱，能够读取工作区、修改文件及访问网络；审批界面会展示解释器、相对脚本、逐项转义参数和该风险，每次调用都重新确认。
- `install_skill` 只接受公开 `github.com` HTTPS Skill 目录和 `~/.codex/skills` 直属目录；不读取私有仓库凭据，不覆盖同名目标，不执行安装包中的脚本或安装依赖。安装审批与脚本审批相互独立。
- Git 写工具仅存在于 TUI：Stage 不接受目录、删除、glob 或全仓库参数；Commit 只使用当前 Index 并关闭 hooks/GPG；Push 只使用当前分支已有的 HTTPS/SSH 上游，不支持 force、Tag、删远端或设置上游。审批后状态变化会返回冲突。
- 达到上限时 Web UI 以流内错误事件返回，TUI 显示安全错误。已确认并完成的磁盘修改不会因后续模型失败自动回滚，界面会列出仍保留的相对路径。
