# Troubleshooting

## `python3` 无法导入 FastAPI

先检查解释器：

```bash
which python3
python3 --version
python3 -c 'import fastapi'
```

项目要求 Python 3.11。当前机器默认 `/usr/bin/python3` 是 3.9.6 且未安装项目依赖；已验证 `/Users/wangfei/anaconda3/bin/python3` 是 3.11.5 且依赖完整。团队最终环境管理方式待确认。

## `pip check` 报告大量非项目依赖冲突

当前可运行的 Anaconda 3.11 是共享全局环境，`pip check` 会报告 Tables、Spyder、LangChain、NumPy 等与本项目无关或间接相关的冲突。这说明测试可通过，但环境并不干净。

不要忽略或擅自升级全局包。建议在获得确认后建立仓库本地 Python 3.11 `.venv`，只安装 `requirements.txt`，再执行：

```bash
python3 -m pip check
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
```

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
python3 -m compileall -q main.py app tests
python3 -m pip check
git diff --check
git status --short
```

确认 `.env`、`__pycache__/` 和 `.pytest_cache/` 未进入暂存区。
