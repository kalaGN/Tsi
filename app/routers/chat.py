from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictStr, validator

from app.services.aliyun_responses import request_upstream_response


router = APIRouter()


class ChatRequest(BaseModel):
    input: StrictStr

    @validator("input")
    def input_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input must not be blank")
        return value


@router.post("/chat")
async def create_chat(request: ChatRequest):
    status_code, response_body = await request_upstream_response(request.input)
    return JSONResponse(content=response_body, status_code=status_code)
