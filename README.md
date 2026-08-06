# FastAPI Demo

## 安装依赖

项目要求 Python 3.11。先确认当前解释器：

```bash
python3 --version
```

当前机器默认 `/usr/bin/python3` 不满足项目环境；请先激活已安装依赖的 Python 3.11 环境。`/Users/wangfei/anaconda3/bin/python3` 可运行当前测试，但其共享全局环境存在 `pip check` 冲突，不应视为最终可复现方案；建议确认后建立项目专用 `.venv`。

```bash
python3 -m pip install -r requirements.txt
```

## 启动项目

进入项目目录：

```bash
cd /Users/wangfei/study/fastapi/demo
```

启动开发服务器：

```bash
python3 -m uvicorn main:app --reload --env-file .env
```

启动成功后可以访问：

- 首页：http://127.0.0.1:8000/
- Swagger API 文档：http://127.0.0.1:8000/docs
- ReDoc API 文档：http://127.0.0.1:8000/redoc

按 `Ctrl+C` 停止服务器。

## 调用模型接口

启动项目前，设置阿里云 Responses API 的访问密钥：

```bash
export DASHSCOPE_API_KEY='replace-with-real-api-key'
```

真实密钥只能保存在本地环境变量中，不要写入代码、文档或提交到 Git。

服务启动后，调用 `POST /chat`：

```bash
curl --location 'http://127.0.0.1:8000/chat' \
  --header 'Content-Type: application/json' \
  --data '{"input":"你是谁？"}'
```

接口固定使用 `qwen3-max` 模型，并原样返回上游成功响应。

## 运行测试

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
```

禁用插件自动加载可以避免本机全局安装的第三方 Pytest 插件影响项目测试。

## 提交前检查

```bash
python3 -m compileall -q main.py app tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
python3 -m pip check
git diff --check
```

仓库当前没有配置 Formatter、Lint、类型检查或 CI；不要把不存在的命令当作现有质量门禁。
