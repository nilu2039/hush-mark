from collections.abc import Callable
from typing import BinaryIO
from uuid import uuid4

from hushmark_api.overlap import resolve_overlaps
from hushmark_api.recognizers import (
    DetectionCandidate,
    detect_contextual_pii,
    detect_structured_pii,
)
from hushmark_api.schemas import (
    MAX_TEXT_LENGTH,
    AnalyzeAudioResponseV1,
    AnalyzeResponseV1,
    DetectionV1,
)

ContextDetector = Callable[[str, str], list[DetectionCandidate]]
AudioTranscriber = Callable[[BinaryIO, str, str], str]


class NoSpeechDetectedError(Exception):
    pass


class TranscriptTooLargeError(Exception):
    pass


def analyze_document(
    text: str,
    locale: str,
    contextual_detector: ContextDetector = detect_contextual_pii,
) -> AnalyzeResponseV1:
    candidates = detect_structured_pii(text)
    candidates.extend(contextual_detector(text, locale))
    detections = [
        DetectionV1(
            id=f"det_{index}",
            entity_type=candidate.entity_type,
            start=candidate.start,
            end=candidate.end,
            confidence=candidate.confidence,
            source=candidate.source,
        )
        for index, candidate in enumerate(resolve_overlaps(candidates), start=1)
    ]
    return AnalyzeResponseV1(
        analysis_id=f"ana_{uuid4().hex}",
        text_length=len(text),
        detections=detections,
    )


def analyze_audio_document(
    audio: BinaryIO,
    filename: str,
    content_type: str,
    transcriber: AudioTranscriber,
) -> AnalyzeAudioResponseV1:
    transcript = transcriber(audio, filename, content_type)
    if not transcript.strip():
        raise NoSpeechDetectedError
    if len(transcript) > MAX_TEXT_LENGTH:
        raise TranscriptTooLargeError

    analysis = analyze_document(transcript, "en-IN")
    return AnalyzeAudioResponseV1(
        transcript=transcript,
        analysis_id=analysis.analysis_id,
        text_length=analysis.text_length,
        detections=analysis.detections,
    )
