# Domain Glossary

## Responses API

阿里云提供的 OpenAI 兼容模式响应接口。本项目调用地址为：

```text
https://llm-h2k07hgnp4aylibi.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/responses
```

## Chat Completions API

DeepSeek 提供的对话接口。本项目调用固定地址 `https://api.deepseek.com/chat/completions`，将有序中立消息映射为 `messages`，并使用 SSE 流式响应。

## Provider

外部模型协议适配器。当前支持 `aliyun` 和 `deepseek`，由 `LLM_PROVIDER` 在部署级选择；未设置时默认 DeepSeek，HTTP 请求不能动态切换。

## qwen3-max

阿里云 Provider 的默认模型。可以通过 `ALIYUN_MODEL` 在部署环境覆盖，公开 `/chat` 接口不能动态选择。

## deepseek-v4-flash

DeepSeek Provider 的默认模型。可以通过 `DEEPSEEK_MODEL` 在部署环境覆盖。

## DASHSCOPE_API_KEY

阿里云 Provider 的 Bearer Token 环境变量。只允许从进程环境读取，不得写入源代码、Git 管理的文档、日志或测试数据。

## DEEPSEEK_API_KEY

DeepSeek Provider 的 Bearer Token 环境变量，安全规则与 `DASHSCOPE_API_KEY` 相同。

## Upstream

指当前选中的阿里云或 DeepSeek 模型服务。上游响应属于外部、不可信数据，必须检查 HTTP 状态、JSON 格式和文本结构。

## Normalized Text Response

指项目从不同 Provider 的 SSE 事件中增量提取并汇总文本，统一向 HTTP 返回 `{"output_text": "..."}`，向 TUI 实时展示后提交同一完整文本。原始上游结构不会暴露给交互边界。
