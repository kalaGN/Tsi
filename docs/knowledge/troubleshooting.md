# Troubleshooting

## Zsh 启动时提示 `command not found: -e`

原因是 `~/.zshrc` 中存在孤立的 `-e` 行。删除该行并重新加载 Zsh 配置。

## 启动时未读取 `.env`

应用使用 `os.getenv` 读取配置，Uvicorn 必须显式加载环境文件：

```bash
python3 -m uvicorn main:app --reload --env-file .env
```

也可以先在 Shell 中执行：

```bash
export DASHSCOPE_API_KEY='replace-with-real-api-key'
```

## `/chat` 返回 503

确认 `DASHSCOPE_API_KEY` 已设置且不是空字符串。如果使用 `.env`，确认启动命令包含 `--env-file .env`。

## Pytest 启动时出现 LangSmith 或 Pydantic 错误

本机全局安装的第三方 Pytest 插件可能与项目 Pydantic 版本冲突。使用项目约定命令隔离全局插件：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
```

## 上游调用超时

- 确认网络可以访问阿里云上游域名。
- 检查 API Key 是否有效。
- 查看接口返回状态：上游响应超时映射为 `504`，连接失败映射为 `502`。
- 不要在未记录实际耗时证据前盲目增加超时时间。

## 提交前检查

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
python3 -m compileall -q main.py tests
git diff --check
git status --short
```

确认 `.env`、`__pycache__/` 和 `.pytest_cache/` 未进入暂存区。
