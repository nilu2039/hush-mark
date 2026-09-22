from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from hushmark_api.schemas import (
    MAX_TEXT_LENGTH,
    AnalyzeRequestV1,
    AnalyzeResponseV1,
    ErrorResponseV1,
)
from hushmark_api.service import analyze_document

app = FastAPI(title="HushMark API", version="0.1.0")


@app.exception_handler(RequestValidationError)
async def request_validation_error(
    _request: Request, exception: RequestValidationError
) -> JSONResponse:
    errors = exception.errors()
    if any(error["type"] == "string_too_long" for error in errors):
        content = {
            "code": "text_too_large",
            "message": f"Text must contain at most {MAX_TEXT_LENGTH} characters.",
        }
    elif any(error["loc"][-1:] == ("text",) for error in errors):
        content = {
            "code": "invalid_text",
            "message": "Text must be a non-empty string.",
        }
    else:
        content = {
            "code": "invalid_request",
            "message": "Request body is invalid.",
        }
    return JSONResponse(status_code=422, content=content)


@app.post(
    "/v1/analyze",
    response_model=AnalyzeResponseV1,
    responses={422: {"model": ErrorResponseV1}},
)
def analyze_text(request: AnalyzeRequestV1) -> AnalyzeResponseV1:
    return analyze_document(request.text, request.locale)


def main() -> None:
    import uvicorn

    uvicorn.run("hushmark_api.main:app", host="127.0.0.1", port=8000)
