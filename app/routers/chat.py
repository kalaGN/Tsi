from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictStr, validator

from app.runtime.chat import ChatErrorCode, ChatRuntimeError, run_chat


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
    try:
        result = await run_chat(request.input)
    except ChatRuntimeError as exc:
        status_code = _http_status_for_error(exc)
        raise HTTPException(status_code=status_code, detail=exc.user_message) from exc

    return JSONResponse(
        content=result.body,
        status_code=result.upstream_status,
    )


def _http_status_for_error(error: ChatRuntimeError) -> int:
    fixed_statuses = {
        ChatErrorCode.INVALID_INPUT: 422,
        ChatErrorCode.CONFIGURATION: 503,
        ChatErrorCode.TIMEOUT: 504,
        ChatErrorCode.CONNECTION: 502,
        ChatErrorCode.INVALID_RESPONSE: 502,
    }
    if error.code in fixed_statuses:
        return fixed_statuses[error.code]
    if error.upstream_status is not None:
        return error.upstream_status
    return 502
