# 故障排查

## 阿里云返回 `Upstream service returned an invalid response`

先查看 `logs/model-calls.log` 中对应请求是否已获得 HTTP 响应。阿里云 Responses API 的真实 SSE 可能发送空字符串 `response.output_text.delta`；项目会将其作为无内容事件忽略，后续有效文本和 `response.completed` 仍正常处理。非字符串 Delta、缺少完成事件、文本不一致或非法工具调用结构仍会返回该中立错误。

如果升级到包含此兼容修复的版本后仍报错，应按 `request_id` 检查响应状态和 `Content-Type`，不要把上游响应正文、系统提示词或密钥复制到公开日志中。

## 状态栏显示 `AGENTS: error`

TUI 只在启动时读取命令执行目录直属的 `AGENTS.md`。确认它是可读取的 UTF-8 普通文件、正文不超过 32 KiB，且不是同名目录；错误期间普通输入不会调用模型，但 `/clear` 和 `/quit` 仍可使用。修复文件后重启 TUI，状态栏应显示 `AGENTS: loaded`；删除文件或保留空白文件后重启则显示 `AGENTS: none`。错误信息不会回显文件路径或正文。

`AGENTS.md` 会作为 system 消息发送给外部模型，并进入现有完整请求日志。不要在其中保存密钥、Token 或隐私数据。

## 状态栏显示 `Skills: error`

TUI 启动时读取 `.agents/skills/*/SKILL.md`。检查每个目录名是否与 YAML `name` 完全一致、`description` 是否非空、Frontmatter 是否由 `---` 包围，以及入口和支持文件是否为有界的非符号链接普通文件。任一 Skill 非法会禁用整批项目 Skill，但不会阻止 `AGENTS.md`、普通对话、已有 Workspace 工具和 `install_skill`；手动修复后需重启 TUI。

Skill Catalog、正文、资源和脚本输入输出会随系统提示词、工具结果及后续请求体明文进入 `logs/model-calls.log`。不要把密钥或隐私数据放入 Skill。脚本审批提示没有文件系统或网络沙箱：只有确认脚本及参数可信时才执行。

## `install_skill` 返回错误

- `skill_already_exists`：`.agents/skills/<expected_name>` 已存在。本版本不覆盖或升级；先人工确认已有目录，不要要求模型绕过冲突。
- `skill_source_unavailable`：个人 Codex 直属目录不存在或不可读，或者公开 GitHub Contents API 返回不可用状态。第一版不支持私有仓库、Token、GitHub Enterprise 和任意下载 URL。
- `skill_download_timeout`：匿名 GitHub 获取超过固定时限。确认网络可访问 `api.github.com` 后重试，每次仍需重新审批。
- `skill_package_invalid`：候选包的 YAML、名称、编码、普通文件类型、符号链接、数量或大小不符合现有 Skill 规则。
- `skill_refresh_failed`：目标曾提交但全量 Catalog 刷新失败，安装事务会删除本次目标并继续使用旧 Runtime。

GitHub URL 必须使用 `https://github.com/<owner>/<repo>/tree/<ref>/<skill-directory>`，第一版 `ref` 不能含 `/`。个人来源参数只写 `~/.codex/skills` 下的直属目录名，`expected_name` 必须与候选 `SKILL.md` 的 `name` 完全一致。安装成功后状态栏可立即更新，但必须发送下一条用户消息后模型才能使用新 Skill；当前请求不能直接运行刚安装的脚本。手动修改目录不会触发热刷新。

排查安装时可按同一 request ID 查看 `llm_tool_call`、`llm_tool_approval` 和 `llm_tool_result`：它们分别回答“请求了什么来源”“是否获批”“成功或属于哪类安全失败”。日志会明文保存 URL、个人目录名和目标名称，不记录真实 Home 绝对路径、GitHub 响应正文或凭据。

## 状态栏显示 `Workspace: error`

TUI 启动时只捕获一次命令执行目录。确认该目录存在、是普通目录且不是符号链接；修复后必须重启。错误状态不会退化为不受限文件访问，也不会在界面或工具结果中显示绝对路径。

## 修改审批、冲突或保护路径错误

- `approval_denied`：本次 Diff 被拒绝；拒绝按钮、`n` 和审批界面的 `Esc` 都不会写盘。
- `protected_path`：目标属于 `.env*`、Git、虚拟环境、会话/日志、Rules、依赖文件或 Workspace 安全实现等固定保护区域，不能由提示词关闭。
- `workspace_conflict`：读取后的 SHA-256、精确旧文本、审批后的内容或撤销目标发生变化。重新让模型读取文件并生成新变更，不要绕过冲突覆盖。
- `check_timeout` / `check_unavailable`：固定检查超过 120 秒，或本地 Python/Git 不可用。检查工具不会接受自定义命令作为替代。

审批通过的修改可能在模型后续失败或取消前已经落盘；TUI 会显示“本轮已写入但尚未完成”和相对路径。检查失败也不会自动回滚。需要撤销时在同一 TUI 进程中要求模型调用 `undo_workspace_change`，并再次确认反向 Diff。Journal 只保留最近 10 个批次且不持久化，重启后无法撤销旧记录。

## 消息可以选中但没有进入系统剪贴板

对话记录可直接双击某一可见行进行复制；任意范围、流式临时回答和审批 Diff 则按住鼠标左键拖选，再按 macOS 的 `Cmd+C` 或其他平台的 `Ctrl+C`。项目使用 Textual 内置 OSC 52 剪贴板通道，不调用 `pbcopy`、`xclip` 等系统命令；部分终端会默认禁用 OSC 52，需在终端设置中允许应用访问剪贴板。Textual 8.2.8 明确提示 macOS 自带 Terminal 可能不支持该通道，此时建议使用 iTerm2、Ghostty、Kitty 或 WezTerm，或使用终端自身带修饰键的原生选择复制能力。

## 输入框上方没有持续显示“思考中”

活动栏只在请求 Worker 存活期间显示，并约每 100 ms 刷新。假 Runner 或上游响应非常快时，可能只短暂出现后立即清空；最终耗时仍会写入对话记录。若请求仍在运行但活动栏消失，先确认 transcript 是否出现错误，再检查是否刚按过 Esc 取消请求。

## TUI 没有逐步显示模型回答

流式临时区域只在收到首个文本 Delta 后显示，并约每 100 ms 合并刷新；上游一次返回大块文本或响应很快时，视觉上可能接近一次性完成。工具调用步骤的中间文本会在执行工具前清空，只有最终步骤会留下完整回答。若请求失败或取消，已显示的片段会被清理且不会写入会话历史；可结合 `logs/model-calls.log` 检查是否收到了完整 SSE 响应。

## TUI 输入区域出现 JSON 日志

当前 TUI 启动入口只把模型、HTTP 和工具事件以中文分块写入 `logs/model-calls.log`，不会向 stderr 输出。如果输入框附近仍出现 `llm_http_request`、`llm_tool_call` 等单行 JSON，先确认使用的是最新代码并完全退出后重新启动 TUI；不要同时通过会额外转发旧 stderr 的包装脚本启动。HTTP 服务仍会按设计向 stderr 和本地文件双写日志，其中只有 stderr 保持单行 JSON。

## 上下键不能移动多行输入光标

这是当前 TUI 的明确快捷键约定：Up/Down 被高优先级绑定为输入历史浏览。向上从最新输入开始，向下越过最新记录会恢复浏览前草稿。`/clear` 会清空该历史；多行文本可以粘贴和召回，但垂直光标移动暂不支持。

## `python3` 无法导入 FastAPI

先检查解释器：

```bash
which python3
python3 --version
python3 -c 'import fastapi'
```

项目要求 Python 3.11。使用仓库本地解释器：

```bash
.venv/bin/python --version
.venv/bin/python -c 'import fastapi'
```

## `pip check` 报告大量非项目依赖冲突

共享 Anaconda 环境中的 Tables、Spyder、LangChain、NumPy 等冲突不属于本项目。本仓库已经建立独立 `.venv`，请在该环境执行：

```bash
.venv/bin/python -m pip check
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
```

## 启动时未读取 `.env`

应用使用 `os.getenv` 读取配置。HTTP 服务由 Uvicorn 显式加载环境文件：

```bash
.venv/bin/python -m uvicorn main:app --reload --env-file .env
```

也可以先在 Shell 中设置所需配置。阿里云示例：

```bash
export DASHSCOPE_API_KEY='replace-with-real-api-key'
```

DeepSeek 示例：

```bash
export LLM_PROVIDER=deepseek
export DEEPSEEK_API_KEY='replace-with-real-api-key'
```

TUI 入口会自动加载项目根目录 `.env`，且不会覆盖 Shell 中已存在的变量：

```bash
.venv/bin/python -m app.tui
```

## `/chat` 返回 503

先确认 `LLM_PROVIDER` 是 `aliyun`、`deepseek` 或未设置；未设置时按 DeepSeek 处理。然后检查所选 Provider 的密钥：

- 阿里云：`DASHSCOPE_API_KEY`。
- DeepSeek：`DEEPSEEK_API_KEY`。

显式空白或未知 `LLM_PROVIDER` 也会返回 503。使用 `.env` 启动 HTTP 时必须包含 `--env-file .env`。

## TUI 显示 `Key: missing`

- 确认项目根目录存在 `.env`，而不是放在 `app/` 或 `app/tui/`。
- 确认所选 Provider 对应的 Key 不是空字符串。
- 如果 Shell 中已经导出了同名空值，先取消或重新设置；TUI 不会用 `.env` 覆盖显式环境变量。
- 状态栏只显示配置状态，不会显示密钥内容。

## TUI 无法启动或终端显示异常

先确认解释器和依赖：

```bash
python3 --version
.venv/bin/python -c 'import textual, dotenv'
.venv/bin/python -m app.tui
```

项目要求 Python 3.11，并固定 Textual 8.2.8。请在支持现代 ANSI 控制序列的终端中运行，不要通过不分配 TTY 的管道启动全屏界面。若界面可打开但无法提交，确认输入区已聚焦后按 `Enter`。

## TUI 英文可见但中文输入不可见

项目启动入口会在 Textual 导入前关闭 Kitty 扩展键盘协议，避免“上报所有按键”模式干扰 macOS 中文输入法。正常情况下可以直接在输入框中完成中文组词和输入，不需要额外快捷键。

- 必须使用 `.venv/bin/python -m app.tui` 启动；不要绕过入口直接运行 `app/tui/application.py`。
- 修改启动入口后需要退出并重新启动旧的 TUI 进程。
- 若仍异常，确认终端没有自行强制开启 Kitty 键盘协议，并尝试 iTerm2、Ghostty、Kitty 或 WezTerm。

## TUI 请求卡住或需要退出

- 输入框非空时，第一次按 `Esc` 只清空当前输入，不取消请求也不开始退出计时。
- 输入框为空时，第一次按 `Esc` 会取消当前请求并提示，1.5 秒内再次按 `Esc` 才退出 TUI。
- 输入 `/quit` 会先取消运行中请求再退出。
- TUI 不自动重试；上游最长等待仍受现有 60 秒总超时限制。

## Pytest 启动时出现 LangSmith 或 Pydantic 错误

本机全局安装的第三方 Pytest 插件可能与项目 Pydantic 版本冲突。使用项目约定命令隔离全局插件：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
```

## 上游调用超时

- 确认网络可以访问当前 Provider 的固定上游域名。
- 检查所选 Provider 的 API Key 是否有效。
- 查看接口返回状态：上游响应超时映射为 `504`，连接失败映射为 `502`。
- 不要在未记录实际耗时证据前盲目增加超时时间。

## 提交前检查

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q main.py app tools tests
.venv/bin/python -m pip check
git diff --check
git status --short
```

确认 `.env`、`__pycache__/` 和 `.pytest_cache/` 未进入暂存区。
