# Troubleshooting

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

- 第一次按 `Esc` 会取消当前请求并提示，1.5 秒内再次按 `Esc` 才退出 TUI。
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
.venv/bin/python -m compileall -q main.py app tests
.venv/bin/python -m pip check
git diff --check
git status --short
```

确认 `.env`、`__pycache__/` 和 `.pytest_cache/` 未进入暂存区。
