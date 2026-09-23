import json
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from openai import OpenAIError
from pydantic import ValidationError
from starlette.background import BackgroundTask

from hushmark_api.redaction import (
    AudioProcessingUnavailableError,
    InvalidAudioError,
    InvalidReviewError,
    render_redacted_audio,
)

from hushmark_api.schemas import (
    MAX_TEXT_LENGTH,
    AnalyzeAudioResponseV1,
    AnalyzeRequestV1,
    AnalyzeResponseV1,
    ErrorResponseV1,
    RedactAudioReviewV1,
)
from hushmark_api.service import (
    ContextualAnalysisUnavailableError,
    NoSpeechDetectedError,
    TimingUnavailableError,
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
    elif any(error["loc"][-1:] == ("review",) for error in errors):
        content = {
            "code": "invalid_review",
            "message": "Add a review form field with JSON containing analysisId and detections.",
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


def _review_validation_message(error: ValidationError) -> str:
    issues: set[str] = set()
    for problem in error.errors(include_input=False):
        location = problem["loc"]
        if problem["type"] == "extra_forbidden":
            issues.add("extra")
        elif location and location[-1] == "status":
            issues.add("status")
        elif location and location[-1] == "analysisId":
            issues.add("analysis_id")
        elif location and location[-1] in ("audioStartMs", "audioEndMs"):
            issues.add("range")
        elif location and location[-1] == "id":
            issues.add("id")
        elif not location and problem["type"] == "value_error":
            issues.add("id")
        elif location and location[0] == "detections":
            if len(location) == 1 and problem["type"] == "value_error":
                issues.add("id")
            elif len(location) == 1:
                issues.add("detections")
            elif problem["type"] == "value_error":
                issues.add("range")
            else:
                issues.add("detection")
        else:
            issues.add("format")
    messages = {
        "status": 'Set every detection status to "approved" or "rejected"; "pending" cannot be exported.',
        "extra": "Remove extra review fields. Each detection accepts only id, status, audioStartMs, and audioEndMs.",
        "analysis_id": "Use the analysisId returned by /v1/analyze/audio.",
        "detections": "Provide detections as an array.",
        "id": "Use detection IDs det_1, det_2, etc., in order without gaps or duplicates.",
        "range": "Use integer audioStartMs and audioEndMs with 0 <= audioStartMs < audioEndMs.",
        "detection": "Each detection needs id, status, audioStartMs, and audioEndMs.",
        "format": "Review must contain analysisId and detections.",
    }
    return " ".join(message for key, message in messages.items() if key in issues)


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
    except TimingUnavailableError:
        return _error(503, "timing_unavailable", "Audio timing is unavailable.")
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


@app.post(
    "/v1/redact/audio",
    response_model=None,
    response_class=FileResponse,
    responses={
        200: {"content": {"audio/mpeg": {}}},
        413: {"model": ErrorResponseV1},
        415: {"model": ErrorResponseV1},
        422: {"model": ErrorResponseV1},
        503: {"model": ErrorResponseV1},
    },
)
def redact_audio(
    file: Annotated[UploadFile, File(description="Original completed recording")],
    review: Annotated[str, Form(description="JSON audio review")],
) -> FileResponse | JSONResponse:
    suffix = Path(file.filename or "").suffix.lower().removeprefix(".")
    if suffix not in SUPPORTED_AUDIO_EXTENSIONS:
        return _error(415, "unsupported_audio_type", "Audio format is not supported.")
    if not file.size:
        return _error(422, "invalid_audio", "A non-empty audio file is required.")
    if file.size > MAX_AUDIO_SIZE_BYTES:
        return _error(413, "audio_too_large", "Audio files must be at most 25 MB.")
    try:
        review_data = json.loads(review)
    except ValueError:
        return _error(
            422,
            "invalid_review",
            "Review must be valid JSON. Paste the object without surrounding quotes.",
        )
    if not isinstance(review_data, dict):
        return _error(
            422,
            "invalid_review",
            "Review must be a JSON object, without quotes around the entire object.",
        )
    try:
        parsed_review = RedactAudioReviewV1.model_validate(review_data)
    except ValidationError as error:
        return _error(422, "invalid_review", _review_validation_message(error))
    try:
        output, temporary_directory = render_redacted_audio(
            file.file, suffix, parsed_review
        )
    except InvalidAudioError:
        return _error(422, "invalid_audio", "Audio file is invalid.")
    except InvalidReviewError:
        return _error(
            422,
            "invalid_review",
            "Every audioEndMs must fit within the uploaded recording's duration.",
        )
    except AudioProcessingUnavailableError:
        return _error(
            503,
            "audio_processing_unavailable",
            "Audio processing is temporarily unavailable.",
        )
    return FileResponse(
        output,
        media_type="audio/mpeg",
        filename="redacted.mp3",
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(temporary_directory.cleanup),
    )


def main() -> None:
    import uvicorn

    uvicorn.run("hushmark_api.main:app", host="127.0.0.1", port=8000)
