"""只允许本机访问的 Web UI 页面和 API。"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, StrictBool, StrictStr, validator

from app.runtime.chat import ChatRuntimeError
from app.runtime.context_settings_store import ContextSettingsConflict, ContextSettingsError
from app.runtime.model_budget import strict_json
from app.runtime.model_config_store import ModelConfigConflict, ModelConfigError, ModelConfigValidation
from app.runtime.task_runs import TaskRunConflict, TaskRunError
from app.runtime.model_selection import ModelSelectionError
from app.runtime.personalization_store import (
    MAX_PERSONAL_PROMPT_BYTES,
    PersonalizationConflict,
    PersonalizationError,
)
from app.webui.approvals import WebApprovalConflict, WebApprovalNotFound
from app.webui.directory_picker import (
    DirectoryPickerBusy,
    DirectoryPickerError,
    DirectoryPickerUnavailable,
    pick_project_directory,
)
from app.webui.projects import WebProjectError, WebProjectNotFound
from app.webui.service import WebUiBusyError, WebUiService
from app.webui.sessions import WebSessionNotFound, WebSessionStoreError
from tools import ToolArgumentError
from tools.workspace import WorkspacePathError


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


class SessionRenameRequest(BaseModel):
    title: StrictStr

    @validator("title")
    def title_must_be_valid(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 80:
            raise ValueError("title length is invalid")
        return normalized


class ToolApprovalDecisionRequest(BaseModel):
    request_id: StrictStr
    approved: StrictBool

    @validator("request_id")
    def request_id_must_be_valid(cls, value: str) -> str:
        if not value.strip() or len(value) > 128:
            raise ValueError("request id is invalid")
        return value


def create_webui_router(service: WebUiService | None = None) -> APIRouter:
    """创建 Web Router；生产 Service 延迟到首次 API 请求再装配。"""

    router = APIRouter()
    resolved_service = service

    def current_service() -> WebUiService:
        nonlocal resolved_service
        if resolved_service is None:
            try:
                resolved_service = WebUiService.production()
            except (
                OSError,
                ValueError,
                ChatRuntimeError,
                ContextSettingsError,
                ModelConfigError,
                WebSessionStoreError,
                WebProjectError,
                WebProjectNotFound,
            ) as exc:
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

    @router.get("/ui/tsi-mark.svg", include_in_schema=False)
    async def web_ui_brand_mark(request: Request):
        """提供页面品牌标记与浏览器 favicon。"""

        _require_loopback(request)
        return FileResponse(
            STATIC_ROOT / "tsi-mark.svg",
            media_type="image/svg+xml",
        )

    @router.get("/ui/context-settings.js", include_in_schema=False)
    async def context_settings_javascript(request: Request):
        _require_loopback(request)
        return FileResponse(STATIC_ROOT / "context-settings.js", media_type="application/javascript")

    @router.get("/ui/model-config.js", include_in_schema=False)
    async def model_config_javascript(request: Request):
        _require_loopback(request)
        return FileResponse(STATIC_ROOT / "model-config.js", media_type="application/javascript")

    @router.get("/ui/api/bootstrap")
    async def bootstrap(request: Request):
        _require_loopback(request)
        return current_service().bootstrap()

    @router.get("/ui/api/statistics")
    async def statistics(request: Request):
        _require_loopback(request)
        return current_service().statistics_payload()

    @router.get("/ui/api/context-settings")
    async def context_settings(request: Request):
        _require_loopback(request)
        try:
            return current_service().context_settings_payload()
        except ContextSettingsError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/ui/api/model-config")
    async def model_config(request: Request):
        """仅返回候选模型与 Key 是否配置，绝不回传密钥正文。"""

        _require_loopback(request)
        try:
            return current_service().model_config_payload()
        except (ModelConfigError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="模型配置无法读取。") from exc

    @router.put("/ui/api/model-config")
    async def save_model_config(request: Request):
        """同源有界写入，明确区分保留、替换和删除密钥。"""

        _require_loopback(request)
        _require_same_origin_json(request)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 12 * 1024:
                raise HTTPException(status_code=422, detail="模型配置请求过大。")
            raw.extend(chunk)
        try:
            payload = strict_json(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("invalid payload")
            required = {"expected_revision", "provider", "models", "api_key_action"}
            if payload.get("api_key_action") == "set":
                required.add("api_key")
            if set(payload) != required:
                raise ValueError("invalid fields")
            return current_service().save_model_config(payload)
        except (UnicodeError, ValueError, ModelConfigValidation) as exc:
            raise HTTPException(status_code=422, detail="模型配置无效，请检查输入。") from exc
        except (ModelConfigConflict, WebUiBusyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ModelConfigError as exc:
            raise HTTPException(status_code=503, detail="模型配置保存失败，请检查本机文件。") from exc

    @router.get("/ui/api/personalization")
    async def personalization(request: Request):
        """读取本机 Web 会话共用的自定义系统提示词。"""

        _require_loopback(request)
        try:
            return current_service().personalization_payload()
        except PersonalizationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.put("/ui/api/personalization")
    async def save_personalization(request: Request):
        """严格校验正文与来源，避免覆盖其他标签页的修改。"""

        _require_loopback(request)
        _require_same_origin_json(request)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 20 * 1024:
                raise HTTPException(status_code=422, detail="个性化设置请求过大。")
            raw.extend(chunk)
        try:
            payload = strict_json(raw.decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {"expected_revision", "prompt"}:
                raise ValueError("个性化设置字段无效。")
            revision, prompt = payload["expected_revision"], payload["prompt"]
            if type(revision) is not int or revision < 0 or not isinstance(prompt, str):
                raise ValueError("个性化设置值无效。")
            if len(prompt.encode("utf-8")) > MAX_PERSONAL_PROMPT_BYTES:
                raise ValueError("自定义系统提示词过长。")
            return current_service().save_personalization(expected_revision=revision, prompt=prompt)
        except (UnicodeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="个性化设置无效，请检查内容长度。") from exc
        except PersonalizationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PersonalizationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.put("/ui/api/context-settings")
    async def save_context_settings(request: Request):
        _require_loopback(request)
        _require_same_origin_json(request)
        # 在拼接前逐块限制大小，同时严格拒绝重复键与未知秘密字段。
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 16 * 1024:
                raise HTTPException(status_code=422, detail="上下文设置请求过大。")
            raw.extend(chunk)
        try:
            payload = strict_json(raw.decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("scope") not in {"model", "compaction"}:
                raise ValueError("设置范围无效。")
            keys = {"expected_revision", "scope", "overrides"}
            if payload["scope"] == "model":
                keys |= {"provider", "model"}
                if not all(isinstance(payload.get(key), str) for key in ("provider", "model")):
                    raise ValueError("模型标识无效。")
            if set(payload) != keys or type(payload["expected_revision"]) is not int or payload["expected_revision"] < 0:
                raise ValueError("上下文设置字段无效。")
            return current_service().save_context_settings(payload)
        except (UnicodeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="上下文设置无效，请检查字段范围和预算组合。") from exc
        except (ContextSettingsConflict, WebUiBusyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ContextSettingsError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

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

    @router.get("/ui/api/tasks")
    async def list_tasks(request: Request):
        _require_loopback(request)
        try:
            return {"tasks": current_service().task_payloads()}
        except TaskRunError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/ui/api/tasks")
    async def create_task(request: Request):
        _require_loopback(request)
        _require_same_origin_json(request)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 8 * 1024:
                raise HTTPException(status_code=422, detail="任务请求过大。")
            raw.extend(chunk)
        try:
            payload = strict_json(raw.decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {"goal", "conditions"} or not isinstance(payload["conditions"], list):
                raise ValueError("invalid task payload")
            return current_service().create_task(payload["goal"], payload["conditions"])
        except (UnicodeError, ValueError, TypeError, TaskRunError, WorkspacePathError) as exc:
            raise HTTPException(status_code=422, detail="任务目标或验收条件无效。") from exc
        except WebUiBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/ui/api/tasks/{task_id}")
    async def get_task(request: Request, task_id: str):
        _require_loopback(request)
        try:
            return current_service().task_payload(task_id)
        except TaskRunError as exc:
            raise HTTPException(status_code=404, detail="任务不存在或当前项目不可见。") from exc

    @router.post("/ui/api/tasks/{task_id}/run")
    async def run_task(request: Request, task_id: str):
        _require_loopback(request)
        _require_same_origin_json(request)
        active_service = current_service()
        if not active_service.runtime_info.api_key_configured:
            raise HTTPException(status_code=503, detail="API Key 未配置。")
        if active_service.is_busy:
            raise HTTPException(status_code=409, detail="已有请求正在运行。")
        try:
            task = active_service.task_payload(task_id)
            if task["state"] not in {"ready", "needs_review"}:
                raise HTTPException(status_code=409, detail="任务当前不可执行。")
        except TaskRunError as exc:
            raise HTTPException(status_code=404, detail="任务不存在或当前项目不可见。") from exc

        async def stream():
            try:
                async for event in active_service.stream_task(task_id):
                    yield json.dumps(event, ensure_ascii=False) + "\n"
            except (TaskRunError, TaskRunConflict, WebUiBusyError, ValueError) as exc:
                yield json.dumps({"type": "failed", "message": str(exc)}, ensure_ascii=False) + "\n"

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @router.post("/ui/api/tasks/{task_id}/cancel")
    async def cancel_task(request: Request, task_id: str):
        _require_loopback(request)
        _require_same_origin_json(request)
        return {"cancelled": current_service().cancel_task(task_id)}

    @router.post("/ui/api/cancel")
    async def cancel(request: Request):
        _require_loopback(request)
        return {"cancelled": current_service().cancel_current()}

    @router.post("/ui/api/tool-approvals/{approval_id}")
    async def resolve_tool_approval(
        request: Request,
        approval_id: str,
        payload: ToolApprovalDecisionRequest,
    ):
        _require_loopback(request)
        if not approval_id.strip() or len(approval_id) > 128:
            raise HTTPException(status_code=422, detail="审批参数无效。")
        try:
            return current_service().resolve_tool_approval(
                approval_id,
                payload.request_id,
                payload.approved,
            )
        except WebApprovalNotFound as exc:
            raise HTTPException(status_code=404, detail="审批已失效。") from exc
        except WebApprovalConflict as exc:
            raise HTTPException(status_code=409, detail="审批与当前请求不匹配。") from exc

    @router.post("/ui/api/clear")
    async def clear(request: Request):
        _require_loopback(request)
        return _run_session_action(current_service().clear)

    @router.post("/ui/api/sessions")
    async def create_session(request: Request):
        _require_loopback(request)
        return _run_session_action(current_service().create_session)

    @router.post("/ui/api/projects")
    async def create_project(request: Request):
        _require_loopback(request)
        _require_same_origin_json(request)
        payload = await _project_request(request)
        return _run_session_action(current_service().create_project, project_action=True, **payload)

    @router.post("/ui/api/projects/pick-directory")
    async def choose_project_directory(request: Request):
        """只打开本机目录选择器，回填路径不等于保存项目。"""

        _require_loopback(request)
        _require_same_origin_json(request)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 32:
                raise HTTPException(status_code=422, detail="目录选择请求无效。")
            raw.extend(chunk)
        try:
            if strict_json(raw.decode("utf-8")) != {}:
                raise ValueError("unexpected request body")
        except (UnicodeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="目录选择请求无效。") from exc
        if current_service().is_busy:
            raise HTTPException(status_code=409, detail="已有请求正在运行。")
        try:
            path = await pick_project_directory()
        except DirectoryPickerUnavailable as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        except DirectoryPickerBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DirectoryPickerError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"cancelled": path is None, "path": path}

    @router.patch("/ui/api/projects/{project_id}")
    async def update_project(request: Request, project_id: str):
        _require_loopback(request)
        _require_same_origin_json(request)
        payload = await _project_request(request)
        return _run_session_action(current_service().update_project, project_id, project_action=True, **payload)

    @router.post("/ui/api/projects/{project_id}/select")
    async def select_project(request: Request, project_id: str):
        _require_loopback(request)
        _require_same_origin_json(request)
        return _run_session_action(current_service().select_project, project_id, project_action=True)

    @router.post("/ui/api/sessions/{session_id}/select")
    async def select_session(request: Request, session_id: str):
        _require_loopback(request)
        return _run_session_action(current_service().select_session, session_id)

    @router.patch("/ui/api/sessions/{session_id}")
    async def rename_session(
        request: Request,
        session_id: str,
        payload: SessionRenameRequest,
    ):
        _require_loopback(request)
        return _run_session_action(
            current_service().rename_session,
            session_id,
            payload.title,
        )

    @router.delete("/ui/api/sessions/{session_id}")
    async def delete_session(request: Request, session_id: str):
        _require_loopback(request)
        return _run_session_action(current_service().delete_session, session_id)

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
        except (ToolArgumentError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="工作区文件不可用。") from exc

    @router.get("/ui/api/files/preview")
    async def preview(request: Request, path: str):
        _require_loopback(request)
        try:
            return await current_service().preview_file(path)
        except (ToolArgumentError, ValueError) as exc:
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


def _require_same_origin_json(request: Request) -> None:
    """本机地址不等于同源；浏览器跨站写入和不透明 Origin 一律拒绝。"""

    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=422, detail="设置只接受 JSON。")
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in {"same-origin", "none"}:
        raise HTTPException(status_code=403, detail="不允许跨站修改设置。")
    origin = request.headers.get("origin")
    if origin is None:
        # 无浏览器来源头的本机 JSON 客户端可用；浏览器声明来源时必须精确匹配。
        return
    try:
        source, target = urlsplit(origin), urlsplit(str(request.url))
        def identity(parts):
            return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
        valid = source.scheme in {"http", "https"} and identity(source) == identity(target)
        valid = valid and not source.username and not source.password and source.path in {"", "/"} and not source.query and not source.fragment
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(status_code=403, detail="不允许跨站修改设置。")


async def _project_request(request: Request) -> dict[str, str]:
    """限制项目配置请求大小和字段，不让浏览器提交任意路径参数。"""

    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 4 * 1024:
            raise HTTPException(status_code=422, detail="项目配置请求过大。")
        raw.extend(chunk)
    try:
        payload = strict_json(raw.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"name", "path"}:
            raise ValueError("invalid project fields")
        if not isinstance(payload["name"], str) or not isinstance(payload["path"], str):
            raise ValueError("invalid project values")
    except (UnicodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="项目配置无效。") from exc
    return payload


def _run_session_action(action, *args, project_action: bool = False, **kwargs):
    """把内部会话异常映射为稳定且不泄露路径的 HTTP 错误。"""

    try:
        return action(*args, **kwargs)
    except WebUiBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WebSessionNotFound as exc:
        raise HTTPException(status_code=404, detail="会话不存在。") from exc
    except WebProjectNotFound as exc:
        raise HTTPException(status_code=404, detail="项目不存在。") from exc
    except ValueError as exc:
        detail = str(exc) if project_action else "会话参数无效。"
        raise HTTPException(status_code=422, detail=detail) from exc
    except (WebSessionStoreError, WebProjectError, ChatRuntimeError) as exc:
        raise HTTPException(status_code=503, detail="会话存储不可用。") from exc
