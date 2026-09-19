import asyncio
import json

import httpx
import pytest

from tools.contracts import ToolCall
from tools.registry import ToolRegistry
from tools.web_search import MAX_RESPONSE_BYTES, SERPER_SEARCH_URL, WebSearchTool


def run_search(tool, arguments):
    result = asyncio.run(
        ToolRegistry((tool,)).execute(
            ToolCall("search-1", "web_search", json.dumps(arguments))
        )
    )
    return json.loads(result.output), result


def test_web_search_does_not_request_network_without_api_key():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"organic": []})

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
        ) as client:
            return await run_search_async(
                WebSearchTool(environ={}, client=client),
                {"query": "Tsi"},
            )

    payload, result = asyncio.run(scenario())

    assert result.is_error is False
    assert payload["data"] == {
        "status": "unavailable",
        "reason": "api_key_missing",
    }
    assert requests == []


def test_web_search_calls_only_fixed_serper_endpoint_and_filters_results():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["api_key"] = request.headers["X-API-KEY"]
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "organic": [
                    {
                        "title": "  官方文档  ",
                        "link": "https://example.com/docs",
                        "snippet": "  使用说明  ",
                    },
                    {
                        "title": "不安全链接",
                        "link": "javascript:alert(1)",
                        "snippet": "应忽略",
                    },
                ]
            },
        )

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
        ) as client:
            return await run_search_async(
                WebSearchTool(
                    environ={"SERPER_API_KEY": "test-serper-key"},
                    client=client,
                ),
                {"query": "  Tsi 助手  ", "limit": 2},
            )

    payload, result = asyncio.run(scenario())

    assert result.is_error is False
    assert seen == {
        "url": SERPER_SEARCH_URL,
        "method": "POST",
        "api_key": "test-serper-key",
        "payload": {"q": "Tsi 助手", "num": 2, "gl": "cn", "hl": "zh-cn"},
    }
    assert payload["data"] == {
        "status": "success",
        "results_count": 1,
        "results": [
            {
                "title": "官方文档",
                "link": "https://example.com/docs",
                "snippet": "使用说明",
            }
        ],
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": ""},
        {"query": "x" * 501},
        {"query": "test", "limit": True},
        {"query": "test", "limit": 0},
        {"query": "test", "limit": 11},
        {"query": "test", "url": "https://example.com"},
    ],
)
def test_web_search_rejects_invalid_arguments(arguments):
    payload, result = run_search(WebSearchTool(environ={}), arguments)

    assert result.is_error is True
    assert payload["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(503), "upstream_unavailable"),
        (httpx.Response(200, content=b"not-json"), "invalid_response"),
        (httpx.Response(200, json=[]), "invalid_response"),
        (
            httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)),
            "response_too_large",
        ),
    ],
)
def test_web_search_returns_stable_errors_for_bad_upstream(response, reason):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response),
            trust_env=False,
        ) as client:
            return await run_search_async(
                WebSearchTool(
                    environ={"SERPER_API_KEY": "test-key"},
                    client=client,
                ),
                {"query": "Tsi"},
            )

    payload, result = asyncio.run(scenario())

    assert result.is_error is False
    assert payload["data"] == {"status": "error", "reason": reason}


async def run_search_async(tool, arguments):
    result = await ToolRegistry((tool,)).execute(
        ToolCall("search-1", "web_search", json.dumps(arguments))
    )
    return json.loads(result.output), result
