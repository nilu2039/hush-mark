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


def render_redacted_document(
    text: str, review: RedactDocumentReviewV1, has_bom: bool
) -> bytes:
    if any(detection.end > len(text) for detection in review.detections):
        raise InvalidTextReviewError
    for detection in reversed(review.detections):
        if detection.status == ReviewStatus.APPROVED:
            text = (
                text[: detection.start]
                + f"[{detection.entity_type.value}]"
                + text[detection.end :]
            )
    return (BOM_UTF8 if has_bom else b"") + text.encode("utf-8")
