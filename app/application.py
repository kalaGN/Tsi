"""FastAPI 应用组装入口。"""

import os
import secrets

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import JSONResponse

from app.observability.model_logging import configure_model_logging
from app.webui import create_webui_router


def create_app() -> FastAPI:
    """创建应用并集中注册当前项目的 HTTP 路由。"""

    configure_model_logging()
    application = FastAPI(title="Tsi 助手")
    desktop_token = os.environ.get("TSI_DESKTOP_TOKEN")

    if desktop_token:
        @application.middleware("http")
        async def require_desktop_token(request: Request, call_next):
            """桌面 sidecar 的 API 只接受当前 WebView 持有的启动令牌。"""

            if request.url.path.startswith("/ui/api/"):
                supplied = request.headers.get("x-tsi-desktop-token", "")
                if not secrets.compare_digest(supplied, desktop_token):
                    return JSONResponse(status_code=401, content={"detail": "桌面请求未经授权。"})
            return await call_next(request)

    application.include_router(create_webui_router())

    @application.get("/")
    def read_root():
        return {"Hello": "World"}

    return application


app = create_app()
