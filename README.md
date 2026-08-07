# FastAPI Demo

这是一个轻量的单轮大模型调用项目，同时提供 FastAPI HTTP 接口和 Textual TUI，支持阿里云 Responses API 与 DeepSeek Chat Completions API。

## 安装依赖

项目要求 Python 3.11，当前仓库已使用本地 `.venv`：

```bash
cd /Users/wangfei/study/fastapi/demo
.venv/bin/python --version
.venv/bin/python -m pip install -r requirements.txt
```

## 配置模型

配置写入项目根目录 `.env`。该文件已被 Git 忽略，真实密钥不得写入代码、文档或提交记录。

DeepSeek 是默认 Provider；`LLM_PROVIDER` 可以省略：

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=replace-with-real-api-key
DEEPSEEK_MODEL=deepseek-v4-flash
```

使用阿里云时必须显式选择：

```dotenv
LLM_PROVIDER=aliyun
DASHSCOPE_API_KEY=replace-with-real-api-key
ALIYUN_MODEL=qwen3-max
```

`ALIYUN_MODEL`、`DEEPSEEK_MODEL` 都是可选项，空白或未设置时使用示例中的默认模型。Provider 只能为 `aliyun` 或 `deepseek`；显式空白或其他值会返回配置错误。

## 启动 HTTP 服务

```bash
.venv/bin/python -m uvicorn main:app --reload --env-file .env
```

启动后可访问：

- 首页：http://127.0.0.1:8000/
- Swagger：http://127.0.0.1:8000/docs
- ReDoc：http://127.0.0.1:8000/redoc

调用统一模型接口：

```bash
curl --location 'http://127.0.0.1:8000/chat' \
  --header 'Content-Type: application/json' \
  --data '{"input":"你是谁？"}'
```

无论使用哪个 Provider，成功响应都是：

```json
{
  "output_text": "模型生成的文本"
}
```

接口不再返回阿里云或 DeepSeek 的原始响应字段。

## 启动 TUI

TUI 会从项目根目录 `.env` 加载配置，Shell 中已设置的环境变量优先。无需先启动 Uvicorn：

```bash
.venv/bin/python -m app.tui
```

每次提交都是独立单轮请求；界面可以连续展示结果，但不会把历史消息再次发送给模型。成功或失败后会显示耗时，取消请求不显示耗时。

- `Enter`：发送输入。
- `Esc`：第一次取消运行中请求并提示，1.5 秒内再次按下退出。
- `/clear`：清空界面记录。
- `/quit`：取消运行中请求并退出。

TUI 支持直接使用中文输入法。`/help` 和 `/chat` 不是本地命令，会作为普通文本发送给模型。当前不支持流式输出、多轮记忆、会话持久化、工具调用或请求级模型切换。

## 模型请求日志

HTTP 和 TUI 每次模型调用会写入关联的 `llm_request` 和 `llm_response` 单行 JSON 日志。日志同时输出到 stderr 和：

```text
logs/model-calls.log
```

`llm_request` 包含完整输入正文，`llm_response` 包含完整模型回答，两者使用同一 request ID。日志不记录环境 API Key、Provider 原始响应或异常原文。

> 隐私警告：输入和输出以完整明文同时写入 stderr 和本地文件。不要在提问中粘贴密码、Token、个人隐私或其他不应持久化的数据。具有本地文件读取权限的用户或进程可以读取日志内容。

单文件转储阈值为 10 MiB，保留 5 个备份；`logs/` 已被 Git 忽略。由于正文不截断，单条超大记录可令当前文件暂时超过该阈值。本期不记录耗时或失败事件。

## 运行测试

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
```

所有外部模型测试均使用 HTTPX MockTransport，不会调用真实接口或消耗额度。

## 提交前检查

```bash
.venv/bin/python -m compileall -q main.py app tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
.venv/bin/python -m pip check
git diff --check
```

仓库当前没有 Formatter、Lint、类型检查或 CI，不要把不存在的命令当作现有质量门禁。
