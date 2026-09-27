import json
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from openai import OpenAIError
from pydantic import ValidationError
from starlette.background import BackgroundTask

from hushmark_api.documents import (
    DocumentTextTooLargeError,
    DocumentTooLargeError,
    InvalidTextFileError,
    InvalidTextReviewError,
    SUPPORTED_TEXT_EXTENSIONS,
    decode_text_document,
    render_redacted_document,
    render_redacted_text,
)
from hushmark_api.redaction import (
    AudioProcessingUnavailableError,
    InvalidAudioError,
    InvalidReviewError,
    render_redacted_audio,
)

from hushmark_api.schemas import (
    MAX_TEXT_LENGTH,
    AnalyzeAudioResponseV1,
    AnalyzeDocumentResponseV1,
    AnalyzeRequestV1,
    AnalyzeResponseV1,
    ErrorResponseV1,
    RedactAudioReviewV1,
    RedactDocumentReviewV1,
    RedactTextRequestV1,
)
from hushmark_api.service import (
    ContextualAnalysisUnavailableError,
    NoSpeechDetectedError,
    TimingUnavailableError,
    TranscriptTooLargeError,
    analyze_audio_document,
    analyze_document,
    analyze_text_document,
)
from hushmark_api.transcription import (
    MAX_AUDIO_SIZE_BYTES,
    SUPPORTED_AUDIO_EXTENSIONS,
    transcribe_audio,
)

app = FastAPI(title="HushMark API", version="0.1.0")


@app.exception_handler(RequestValidationError)
async def request_validation_error(
    request: Request, exception: RequestValidationError
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
        if request.url.path.endswith("/document"):
            content = {
                "code": "invalid_text_file",
                "message": "A non-empty text file is required.",
            }
        else:
            content = {
                "code": "invalid_audio",
                "message": "A non-empty audio file is required.",
            }
    elif any("review" in error["loc"] for error in errors):
        content = {
            "code": "invalid_review",
            "message": (
                "Review must contain analysisId and valid detections."
                if request.url.path == "/v1/redact"
                else "Add a review form field with JSON containing analysisId and detections."
            ),
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


@app.post(
    "/v1/redact",
    response_model=None,
    response_class=PlainTextResponse,
    responses={422: {"model": ErrorResponseV1}},
)
def redact_text(request: RedactTextRequestV1) -> PlainTextResponse | JSONResponse:
    try:
        output = render_redacted_text(request.text, request.review)
    except InvalidTextReviewError:
        return _error(
            422,
            "invalid_review",
            "Every detection span must fit within the submitted text.",
        )
    return PlainTextResponse(output, headers={"Cache-Control": "no-store"})


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"code": code, "message": message}
    )


def _document_error(
    error: DocumentTooLargeError | InvalidTextFileError | DocumentTextTooLargeError,
) -> JSONResponse:
    if isinstance(error, DocumentTooLargeError):
        return _error(413, "text_file_too_large", "Text files must be at most 256 KiB.")
    if isinstance(error, DocumentTextTooLargeError):
        return _error(
            422,
            "text_too_large",
            f"Text must contain at most {MAX_TEXT_LENGTH} characters.",
        )
    return _error(422, "invalid_text_file", "A non-empty UTF-8 text file is required.")


@app.post(
    "/v1/analyze/document",
    response_model=AnalyzeDocumentResponseV1,
    responses={
        413: {"model": ErrorResponseV1},
        415: {"model": ErrorResponseV1},
        422: {"model": ErrorResponseV1},
        503: {"model": ErrorResponseV1},
    },
)
def analyze_document_file(
    file: Annotated[UploadFile, File(description="UTF-8 text document")],
) -> AnalyzeDocumentResponseV1 | JSONResponse:
    suffix = Path(file.filename or "").suffix.lower().removeprefix(".")
    if suffix not in SUPPORTED_TEXT_EXTENSIONS:
        return _error(415, "unsupported_text_type", "Text format is not supported.")
    try:
        return analyze_text_document(file.file)
    except (DocumentTooLargeError, InvalidTextFileError, DocumentTextTooLargeError) as error:
        return _document_error(error)
    except ContextualAnalysisUnavailableError:
        return _error(
            503,
            "analysis_unavailable",
            "Contextual analysis is temporarily unavailable.",
        )


@app.post(
    "/v1/redact/document",
    response_model=None,
    response_class=Response,
    responses={
        200: {"content": {"text/plain": {}, "text/markdown": {}}},
        413: {"model": ErrorResponseV1},
        415: {"model": ErrorResponseV1},
        422: {"model": ErrorResponseV1},
    },
)
def redact_document_file(
    file: Annotated[UploadFile, File(description="Original UTF-8 text document")],
    review: Annotated[str, Form(description="JSON document review")],
) -> Response | JSONResponse:
    extension = Path(file.filename or "").suffix.removeprefix(".")
    suffix = extension.lower()
    if suffix not in SUPPORTED_TEXT_EXTENSIONS:
        return _error(415, "unsupported_text_type", "Text format is not supported.")
    try:
        text, has_bom = decode_text_document(file.file)
    except (DocumentTooLargeError, InvalidTextFileError, DocumentTextTooLargeError) as error:
        return _document_error(error)
    try:
        parsed_review = RedactDocumentReviewV1.model_validate(json.loads(review))
    except (ValueError, ValidationError):
        return _error(
            422,
            "invalid_review",
            "Review must contain analysisId and unique detection IDs with valid "
            "types, ranges, and approved or rejected statuses; approved spans "
            "cannot overlap.",
        )
    try:
        output = render_redacted_document(text, parsed_review, has_bom)
    except InvalidTextReviewError:
        return _error(
            422,
            "invalid_review",
            "Every detection span must fit within the uploaded text.",
        )
    media_type = "text/plain" if suffix == "txt" else "text/markdown"
    return Response(
        output,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="redacted.{extension}"',
        },
    )


def _review_validation_message(error: ValidationError) -> str:
    issues: set[str] = set()
    for problem in error.errors(include_input=False):
        location = problem["loc"]
        if problem["type"] == "extra_forbidden":
            issues.add("extra")
        elif problem["type"] == "manual_type_required":
            issues.add("type")
        elif location and location[-1] == "status":
            issues.add("status")
        elif location and location[-1] == "type":
            issues.add("type")
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
        "extra": "Remove extra review fields. Each detection accepts only id, type, status, audioStartMs, and audioEndMs.",
        "analysis_id": "Use the analysisId returned by /v1/analyze/audio.",
        "detections": "Provide detections as an array.",
        "id": "Use unique det_N or man_N IDs; automatic det_N IDs must have no gaps or duplicates.",
        "type": "Use a valid PII type for manual detections and any typed automatic detections.",
        "range": "Use integer audioStartMs and audioEndMs with 0 <= audioStartMs < audioEndMs.",
        "detection": "Each detection needs id, status, audioStartMs, and audioEndMs; manual detections also need type.",
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
