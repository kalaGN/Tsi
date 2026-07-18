import os
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictStr, validator


UPSTREAM_RESPONSES_URL = (
    "https://llm-h2k07hgnp4aylibi.cn-beijing.maas.aliyuncs.com/"
    "compatible-mode/v1/responses"
)
UPSTREAM_MODEL = "qwen3-max"
UPSTREAM_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

app = FastAPI()


class Item(BaseModel):
    name: str
    price: float
    is_offer: bool | None = None


class ChatRequest(BaseModel):
    input: StrictStr

    @validator("input")
    def input_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input must not be blank")
        return value


async def request_upstream_response(input_text: str) -> tuple[int, Any]:
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key or not api_key.strip():
        raise HTTPException(
            status_code=503,
            detail="Upstream API key is not configured",
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"model": UPSTREAM_MODEL, "input": input_text}

    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            response = await client.post(
                UPSTREAM_RESPONSES_URL,
                headers=headers,
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504,
            detail="Upstream request timed out",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to connect to upstream service",
        ) from exc

    if response.status_code in (401, 403):
        raise HTTPException(
            status_code=response.status_code,
            detail="Upstream authentication failed",
        )
    if not response.is_success:
        raise HTTPException(
            status_code=response.status_code,
            detail="Upstream service returned an error",
        )

    try:
        return response.status_code, response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="Upstream service returned invalid JSON",
        ) from exc


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/items/{item_id}")
def read_item(item_id: int, q: str | None = None):
    return {"item_id": item_id, "q": q}


@app.put("/items/{item_id}")
def update_item(item_id: int, item: Item):
    return {"item_name": item.name, "item_id": item_id}


@app.post("/chat")
async def create_chat(request: ChatRequest):
    status_code, response_body = await request_upstream_response(request.input)
    return JSONResponse(content=response_body, status_code=status_code)
