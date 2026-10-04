# 运行与运维

桌面打包、HTTP 服务端点与模型日志说明。模型 Key、会话与个人配置的存储说明见[使用指南](usage.md)。

[返回文档首页](../README.md) · [项目首页](../../README.md)

## 打包 macOS 桌面应用

正式桌面包使用 Tauri 2 容纳现有 Web UI，PyInstaller 内置 FastAPI 后端；双击 `.app` 即可独立运行，不需要另起 Uvicorn，也不需要 Node.js。当前构建脚本面向 Apple Silicon macOS，需要 Rust/Cargo、Python 3.11 和 Xcode Command Line Tools。

```bash
cd Tsi
.venv/bin/python -m pip install -r requirements-desktop.txt
cargo install tauri-cli --version '^2.0.0' --locked
bash scripts/build_macos_tauri.sh
```

构建产物位于 `src-tauri/target/release/bundle/macos/Tsi.app`。也可以把 Cargo CLI 装在项目本地的 `build/tauri-cli`，构建脚本会自动识别。构建产物与中间文件不受 Git 跟踪。当前产物仅供本机验证，未配置 Apple Developer ID 签名与公证；分发给其他 Mac 前仍需完成签名和公证。

桌面版首次运行后，在应用内「设置 → 模型」配置 API Key。桌面数据、日志和默认工作区分别保存在 `~/Library/Application Support/Tsi/` 的 `data/`、`logs/`、`workspace/`，与源码运行模式隔离。桌面后端只绑定随机本机端口；桌面 API 还需启动时生成的临时令牌。旧 Pake 方案仅是依赖外部 Uvicorn 的网页壳，不属于独立打包方案。

## 服务端点

Web UI 与 FastAPI 由同一个 Uvicorn 进程提供：

```bash
.venv/bin/python -m uvicorn main:app --reload
```

启动后可访问：

- 首页：http://127.0.0.1:8000/
- Web UI：http://127.0.0.1:8000/ui
- Swagger：http://127.0.0.1:8000/docs
- ReDoc：http://127.0.0.1:8000/redoc

所有接口只接受 loopback 客户端。无状态聚合 JSON 的 `POST /chat` 接口已移除，对话只能通过 Web UI 或 TUI 发起。

## 模型请求日志

Web 服务每次模型调用会把同一 request ID 关联的事件写入 stderr 和本地文件；stderr 保持单行 JSON，本地文件使用适合直接阅读的中文分块格式。TUI 为避免日志覆盖全屏界面，只写本地文件：

```text
logs/
├── runtime/YYYYMMDD-model-calls.log  # Web 服务与 TUI 真实使用日志
└── tests/YYYYMMDD-model-calls.log    # Pytest 测试日志
```

文件名使用进程启动时的北京日期，例如 `20260920-model-calls.log`。旧的 `logs/model-calls.log*` 不会自动迁移或删除，仅作历史记录保留。

本地文件示例：

```text
时间：2026-08-12 14:32:19.311 +08:00
事件：工具结果
请求ID：b7102d8c...
调用ID：call_01
工具：read_workspace_file
状态：成功
耗时：2.73 ms
输出长度：1256 字符

【工具输出】
{
  "ok": true,
  "data": {
    "path": "README.md"
  }
}
================================================================================
```

文件时间统一使用北京时间并包含毫秒和 `+08:00`；请求体、Header、超时配置、工具参数和工具结果会在可解析时缩进为 JSON，模型输入输出保持原始换行。

不需要工具且上游返回 usage 时，成功调用按固定顺序产生五条可关联事件：

```text
llm_request -> llm_http_request -> llm_http_response -> llm_token_usage -> llm_response
```

如果上游没有返回 usage，则省略 `llm_token_usage`，其余成功事件不变。

- `llm_request`：Runtime 视角的当前输入正文（仅最后一条 user 消息）。
- `llm_http_request`：真实外部 HTTP 边界，记录实际 Provider URL、`POST`、脱敏 Header（`Authorization` 固定写为 `Bearer [REDACTED]`）、完整 JSON 请求体和 `connect_seconds=10 / total_seconds=600` 超时。完整请求体包含 DeepSeek 的 `messages` 或阿里云的 `input`，多轮历史以明文按上游顺序完整保留。
- `llm_http_response`：外部 HTTP 收到响应后立即记录，包含状态码（含非 2xx）、Content-Type 和单调时钟耗时 `duration_ms`，不记录原始响应体。
- `llm_token_usage`：每个成功解析的模型步骤各记录一次，包含同一 request ID、从 1 开始的步骤编号，以及输入、输出和总 Token；工具循环会产生多条，日志不保存 Provider 私有 usage 对象。
- `llm_response`：成功统一输出文本。
- `llm_error`：Runtime 最终失败，包含同一 request ID、Provider、模型、安全错误代码、异常类型、上游状态和可展示文案。它可以区分“HTTP 已响应但内容语义无效”和网络失败。

需要工具时，同一个 request ID 下会出现多组 `llm_http_request/llm_http_response`，并在工具执行处插入：

- `llm_tool_call`：call ID、工具名、参数字符数和完整 JSON 参数。
- `llm_tool_result`：call ID、成功/错误状态、耗时、输出字符数和完整工具结果。

工具参数和结果会直接记录在专用工具事件中；实际回传模型的完整工具结果还会出现在下一次 `llm_http_request.request_body` 中。审批事件只记录决定、文件数和 Diff 字符数，不额外记录审批正文。调用 `install_skill` 时，GitHub URL、个人 Skill 目录名、目标名称和安全安装结果也会作为工具参数及结果明文记录，但真实 Home 绝对路径、GitHub 响应正文和凭据不会记录。

本轮 Token 合计是同一请求内所有模型步骤 usage 的逐项相加。它表示已经发生的上游消耗，不等于下一次请求的上下文窗口占比；当前不持久化会话累计、不换算费用，也不单独展示缓存或推理 Token。

连接超时或网络失败时，`llm_http_request` 后写一条 `llm_http_error`，随后 Runtime 写 `llm_error`。非 2xx 或语义无效响应会先写 `llm_http_response`，随后写 `llm_error`；后者保存最多 256 KiB 的上游原始响应体前缀，并标明是否截断，不记录响应 Header 或 Traceback。

日志不记录环境 API Key、真实 `Authorization`、响应 Header、Cookie 或异常堆栈。失败事件会按上述边界记录上游原始响应体，其中可能包含模型生成内容或上游诊断信息。

> 隐私警告：输入、输出、工具参数、工具结果、TUI 加载的 `AGENTS.md`、Skill Catalog/正文/文本资源、脚本参数及 stdout/stderr 和完整请求体都会以明文写入本地文件；Web 服务还会同步写入 stderr，且多轮历史会在每次调用时重复落盘。不要在这些位置放置密码、Token、个人隐私或其他不应发送和持久化的数据。具有本地文件读取权限的用户或进程可以读取日志内容。

运行与测试日志独立轮转：单文件阈值为 10 MiB，各保留 5 个备份；`logs/` 已被 Git 忽略。由于正文不截断，单条超大记录可令当前文件暂时超过该阈值。日志失败不影响模型请求本身。

### Agent 链路与页面诊断

Web/TUI 每次用户请求生成一个 `request_id`，预判和正式执行共用此 ID。摘要保留所属请求 ID 并通过子 `span_id / parent_span_id` 区分；日志自动补充 `session_id`、可用时的 `task_id`，工具事件包含 `call_id`。同一请求的所有阶段可按 `root_request_id` 检索。

- `agent_request_started`：入口、Provider、模型和接收阶段。
- `agent_phase_changed`：新阶段、上一阶段和上一阶段耗时。阶段为 `task_preflight`、`plan_confirmation`、`context_preparation`、`model_call`、`tool_approval`、`tool_execution`、`session_save`。
- `agent_request_finished`：唯一终态 `completed / failed / cancelled / disconnected`、最后阶段、安全错误码和总耗时；`wait_ms` 是计划确认与工具审批等待，`execution_ms` 是总耗时扣除人工等待。连接关闭且执行未结束时取消执行并记录 `disconnected`；主动取消记录 `cancelled`，两者不能混淆。

Web 终态事件统一触发统计与生命周期日志；完成载荷构造失败时记为失败，不提前计入成功。每轮只记录一次统计，断连在统计中归为取消，在生命周期日志中保留 `disconnected`。

页面通过本机同源 `POST /ui/api/diagnostics` 上报固定分类，独立保存到 `logs/runtime/YYYYMMDD-web-client.log`（桌面版位于应用私有 logs 目录，测试写入 `logs/tests/`）。北京时间，JSON 行格式，10 MiB 轮转和 5 个备份。

记录类别：脚本未处理错误、Promise 未处理拒绝、请求失败、NDJSON 解析失败、无终态断流、终态已收到/已应用，以及终态后仍忙碌。仅允许关联 ID、固定错误类型、最后事件、页面协议版本、有限耗时和已知脚本行列；不接收错误 message/stack、URL、聊天正文或密钥。请求上限 2 KiB，服务端每分钟 60 次，页面每分钟 30 次；上报失败不影响聊天。首次事件前的连接失败没有后端请求 ID，仍只能按时间定位。

排查顺序：按 `request_id` 查模型日志的最后阶段和唯一终态，再查同 ID 的页面日志。后端已结束且页面存在 `terminal_state_stuck` 表示 UI 状态异常；页面仅有 `stream_disconnected` 表示未消费到完整终态。工具内部堆栈与 Web 未知异常堆栈仍不在本次诊断范围内。
