from fastapi import FastAPI

from app.routers.chat import router as chat_router


def create_app() -> FastAPI:
    application = FastAPI()
    application.include_router(chat_router)

    @application.get("/")
    def read_root():
        return {"Hello": "World"}

    return application


app = create_app()
