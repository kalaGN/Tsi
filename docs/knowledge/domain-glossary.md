# Domain Glossary

## Responses API

阿里云提供的 OpenAI 兼容模式响应接口。本项目调用地址为：

```text
https://llm-h2k07hgnp4aylibi.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/responses
```

## qwen3-max

本项目固定使用的上游模型名称。当前公开 `/chat` 接口不允许调用方选择其他模型。

## DASHSCOPE_API_KEY

上游服务的 Bearer Token 环境变量。只允许从进程环境读取，不得写入源代码、Git 管理的文档、日志或测试数据。

## Upstream

指 FastAPI 服务调用的阿里云 Responses API。上游响应属于外部、不可信数据，必须检查 HTTP 状态和 JSON 格式。

## Transparent Success Response

指服务不提取或重组模型回答字段，而是将上游成功 JSON 作为整体返回。错误响应不属于透明透传范围，必须经过安全处理。
