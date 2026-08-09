"""HTTP 对话路由及 Runtime 错误到 HTTP 状态码的映射。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, StrictStr, validator

from app.runtime.chat import ChatErrorCode, ChatRuntimeError, run_chat


router = APIRouter()


class ChatRequest(BaseModel):
    """对外 HTTP 请求模型，在进入 Runtime 前完成输入校验。"""

    input: StrictStr

    @validator("input")
    def input_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input must not be blank")
        return value


class ChatResponse(BaseModel):
    """屏蔽上游协议差异的稳定成功响应。"""

    output_text: str


@router.post("/chat", response_model=ChatResponse)
async def create_chat(request: ChatRequest) -> ChatResponse:
    try:
        result = await run_chat(request.input)
    except ChatRuntimeError as exc:
        status_code = _http_status_for_error(exc)
        raise HTTPException(status_code=status_code, detail=exc.user_message) from exc

    return ChatResponse(output_text=result.output_text)


def _http_status_for_error(error: ChatRuntimeError) -> int:
    """将界面无关的 Runtime 错误转换为稳定的 HTTP 状态码。"""

    fixed_statuses = {
        ChatErrorCode.INVALID_INPUT: 422,
        ChatErrorCode.CONFIGURATION: 503,
        ChatErrorCode.TIMEOUT: 504,
        ChatErrorCode.CONNECTION: 502,
        ChatErrorCode.INVALID_RESPONSE: 502,
        ChatErrorCode.TOOL_LIMIT: 502,
    }
    if error.code in fixed_statuses:
        return fixed_statuses[error.code]
    # 鉴权和其他上游失败保留原状态码，但错误正文仍由 Runtime 脱敏。
    if error.upstream_status is not None:
        return error.upstream_status
    return 502
