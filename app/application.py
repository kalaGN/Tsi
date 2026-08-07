"""FastAPI 应用组装入口。"""

from fastapi import FastAPI

from app.observability.model_logging import configure_model_logging
from app.routers.chat import router as chat_router


def create_app() -> FastAPI:
    """创建应用并集中注册当前项目的 HTTP 路由。"""

    configure_model_logging()
    application = FastAPI()
    application.include_router(chat_router)

    @application.get("/")
    def read_root():
        return {"Hello": "World"}

    return application


app = create_app()
