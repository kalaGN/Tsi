"""只允许本机访问的 Web UI 页面和 API。"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, StrictStr, validator

from app.runtime.chat import ChatRuntimeError
from app.runtime.model_selection import ModelSelectionError
from app.webui.service import WebUiBusyError, WebUiService
from tools import ToolArgumentError


STATIC_ROOT = Path(__file__).resolve().parent / "static"


class WebChatRequest(BaseModel):
    input: StrictStr

    @validator("input")
    def input_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input must not be blank")
        if len(value) > 64 * 1024:
            raise ValueError("input is too large")
        return value


class ModelSelectionRequest(BaseModel):
    provider: StrictStr
    model: StrictStr


def create_webui_router(service: WebUiService | None = None) -> APIRouter:
    """创建 Web Router；生产 Service 延迟到首次 API 请求再装配。"""

    router = APIRouter()
    resolved_service = service

    def current_service() -> WebUiService:
        nonlocal resolved_service
        if resolved_service is None:
            try:
                resolved_service = WebUiService.production()
            except (OSError, ValueError, ChatRuntimeError) as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Web UI 启动配置不可用。",
                ) from exc
        return resolved_service

    @router.get("/ui", include_in_schema=False)
    async def web_ui(request: Request):
        _require_loopback(request)
        return FileResponse(STATIC_ROOT / "index.html", media_type="text/html")

    @router.get("/ui/app.css", include_in_schema=False)
    async def web_ui_css(request: Request):
        _require_loopback(request)
        return FileResponse(STATIC_ROOT / "app.css", media_type="text/css")

    @router.get("/ui/app.js", include_in_schema=False)
    async def web_ui_js(request: Request):
        _require_loopback(request)
        return FileResponse(
            STATIC_ROOT / "app.js",
            media_type="application/javascript",
        )

    @router.get("/ui/api/bootstrap")
    async def bootstrap(request: Request):
        _require_loopback(request)
        return current_service().bootstrap()

    @router.post("/ui/api/chat")
    async def chat(request: Request, payload: WebChatRequest):
        _require_loopback(request)
        active_service = current_service()
        if not active_service.runtime_info.api_key_configured:
            raise HTTPException(status_code=503, detail="API Key 未配置。")
        if active_service.is_busy:
            raise HTTPException(status_code=409, detail="已有请求正在运行。")

        async def stream():
            try:
                async for event in active_service.stream_message(payload.input):
                    yield json.dumps(event, ensure_ascii=False) + "\n"
            except WebUiBusyError as exc:
                yield json.dumps(
                    {"type": "failed", "message": str(exc)},
                    ensure_ascii=False,
                ) + "\n"

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @router.post("/ui/api/cancel")
    async def cancel(request: Request):
        _require_loopback(request)
        return {"cancelled": current_service().cancel_current()}

    @router.post("/ui/api/clear")
    async def clear(request: Request):
        _require_loopback(request)
        try:
            current_service().clear()
        except (WebUiBusyError, ChatRuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"cleared": True, "context_percent": 0.0}

    @router.post("/ui/api/model")
    async def select_model(request: Request, payload: ModelSelectionRequest):
        _require_loopback(request)
        try:
            return current_service().select_model(payload.provider, payload.model)
        except WebUiBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ModelSelectionError as exc:
            raise HTTPException(status_code=422, detail=exc.user_message) from exc

    @router.get("/ui/api/files")
    async def files(request: Request):
        _require_loopback(request)
        try:
            return await current_service().list_files()
        except ToolArgumentError as exc:
            raise HTTPException(status_code=422, detail="工作区文件不可用。") from exc

    @router.get("/ui/api/files/preview")
    async def preview(request: Request, path: str):
        _require_loopback(request)
        try:
            return await current_service().preview_file(path)
        except ToolArgumentError as exc:
            raise HTTPException(status_code=404, detail="文件不可读取。") from exc

    return router


def _require_loopback(request: Request) -> None:
    """阻止 Web UI 被误绑定到局域网后远程访问本地会话与文件。"""

    host = request.client.host if request.client is not None else ""
    if host == "testclient":
        return
    try:
        is_loopback = ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise HTTPException(status_code=403, detail="Web UI 仅允许本机访问。")
