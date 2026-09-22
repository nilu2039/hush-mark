from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from openai import OpenAIError

from hushmark_api.schemas import (
    MAX_TEXT_LENGTH,
    AnalyzeAudioResponseV1,
    AnalyzeRequestV1,
    AnalyzeResponseV1,
    ErrorResponseV1,
)
from hushmark_api.service import (
    ContextualAnalysisUnavailableError,
    NoSpeechDetectedError,
    TranscriptTooLargeError,
    analyze_audio_document,
    analyze_document,
)
from hushmark_api.transcription import (
    MAX_AUDIO_SIZE_BYTES,
    SUPPORTED_AUDIO_EXTENSIONS,
    transcribe_audio,
)

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
    elif any(error["loc"][-1:] == ("file",) for error in errors):
        content = {
            "code": "invalid_audio",
            "message": "A non-empty audio file is required.",
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
    responses={422: {"model": ErrorResponseV1}, 503: {"model": ErrorResponseV1}},
)
def analyze_text(request: AnalyzeRequestV1) -> AnalyzeResponseV1 | JSONResponse:
    try:
        return analyze_document(request.text, request.locale)
    except ContextualAnalysisUnavailableError:
        return _error(
            503,
            "analysis_unavailable",
            "Contextual analysis is temporarily unavailable.",
        )


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"code": code, "message": message}
    )


@app.post(
    "/v1/analyze/audio",
    response_model=AnalyzeAudioResponseV1,
    responses={
        413: {"model": ErrorResponseV1},
        415: {"model": ErrorResponseV1},
        422: {"model": ErrorResponseV1},
        503: {"model": ErrorResponseV1},
    },
)
def analyze_audio(
    file: Annotated[UploadFile, File(description="Completed audio recording")],
) -> AnalyzeAudioResponseV1 | JSONResponse:
    suffix = Path(file.filename or "").suffix.lower().removeprefix(".")
    if suffix not in SUPPORTED_AUDIO_EXTENSIONS:
        return _error(415, "unsupported_audio_type", "Audio format is not supported.")
    if not file.size:
        return _error(422, "invalid_audio", "A non-empty audio file is required.")
    if file.size > MAX_AUDIO_SIZE_BYTES:
        return _error(413, "audio_too_large", "Audio files must be at most 25 MB.")

    try:
        return analyze_audio_document(
            file.file,
            f"audio.{suffix}",
            file.content_type or "application/octet-stream",
            transcribe_audio,
        )
    except NoSpeechDetectedError:
        return _error(422, "no_speech_detected", "No speech was detected in the audio.")
    except TranscriptTooLargeError:
        return _error(
            422,
            "transcript_too_large",
            f"Transcript must contain at most {MAX_TEXT_LENGTH} characters.",
        )
    except ContextualAnalysisUnavailableError:
        return _error(
            503,
            "analysis_unavailable",
            "Contextual analysis is temporarily unavailable.",
        )
    except OpenAIError:
        return _error(
            503,
            "transcription_unavailable",
            "Audio transcription is temporarily unavailable.",
        )


def main() -> None:
    import uvicorn

    uvicorn.run("hushmark_api.main:app", host="127.0.0.1", port=8000)
