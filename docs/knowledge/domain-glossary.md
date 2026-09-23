# Domain Glossary

## Responses API

阿里云提供的 OpenAI 兼容模式响应接口。本项目调用地址为：

```text
https://llm-h2k07hgnp4aylibi.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/responses
```

## Chat Completions API

DeepSeek 提供的对话接口。本项目调用固定地址 `https://api.deepseek.com/chat/completions`，将有序中立消息映射为 `messages`，并使用 SSE 流式响应。

## Provider

外部模型协议适配器。当前支持 `aliyun` 和 `deepseek`，由 `LLM_PROVIDER` 在部署级选择；未设置时默认 DeepSeek。

## qwen3-max

阿里云 Provider 的初始候选模型；可在 Web「设置 → 模型」修改。

## deepseek-v4-flash

DeepSeek Provider 的初始候选模型；可在 Web「设置 → 模型」修改。

## DASHSCOPE_API_KEY

旧版阿里云 Provider 的 Bearer Token 环境变量；当前生产 Web/TUI 不再读取它。新密钥在 Web「设置 → 模型」保存到本机私有配置文件，不得写入源码、Git、日志或测试数据。

## DEEPSEEK_API_KEY

旧版 DeepSeek Provider 的 Bearer Token 环境变量；当前生产 Web/TUI 不再读取它，安全规则与阿里云密钥相同。

## Upstream

指当前选中的阿里云或 DeepSeek 模型服务。上游响应属于外部、不可信数据，必须检查 HTTP 状态、JSON 格式和文本结构。

## Normalized Text Response

指项目从不同 Provider 的 SSE 事件中增量提取并汇总文本，向 Web UI 以 NDJSON 事件推送，向 TUI 实时展示后提交同一完整文本。原始上游结构不会暴露给交互边界。
