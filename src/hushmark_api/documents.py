from codecs import BOM_UTF8
from typing import BinaryIO

from hushmark_api.schemas import MAX_TEXT_LENGTH, RedactDocumentReviewV1, ReviewStatus

MAX_DOCUMENT_SIZE_BYTES = 256 * 1024
SUPPORTED_TEXT_EXTENSIONS = frozenset({"txt", "md", "markdown"})


class DocumentTooLargeError(Exception):
    pass


class InvalidTextFileError(Exception):
    pass


class DocumentTextTooLargeError(Exception):
    pass


class InvalidTextReviewError(Exception):
    pass


def decode_text_document(source: BinaryIO) -> tuple[str, bool]:
    content = source.read(MAX_DOCUMENT_SIZE_BYTES + 1)
    if len(content) > MAX_DOCUMENT_SIZE_BYTES:
        raise DocumentTooLargeError
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise InvalidTextFileError from error
    if not text.strip():
        raise InvalidTextFileError
    if len(text) > MAX_TEXT_LENGTH:
        raise DocumentTextTooLargeError
    return text, content.startswith(BOM_UTF8)


def render_redacted_text(text: str, review: RedactDocumentReviewV1) -> str:
    if any(detection.end > len(text) for detection in review.detections):
        raise InvalidTextReviewError
    approved = sorted(
        (
            detection
            for detection in review.detections
            if detection.status == ReviewStatus.APPROVED
        ),
        key=lambda detection: detection.start,
        reverse=True,
    )
    for detection in approved:
        text = (
            text[: detection.start]
            + f"[{detection.entity_type.value}]"
            + text[detection.end :]
        )
    return text


def render_redacted_document(
    text: str, review: RedactDocumentReviewV1, has_bom: bool
) -> bytes:
    output = render_redacted_text(text, review)
    return (BOM_UTF8 if has_bom else b"") + output.encode("utf-8")
