from collections.abc import Callable
from uuid import uuid4

from hushmark_api.overlap import resolve_overlaps
from hushmark_api.recognizers import (
    DetectionCandidate,
    detect_contextual_pii,
    detect_structured_pii,
)
from hushmark_api.schemas import AnalyzeResponseV1, DetectionV1

ContextDetector = Callable[[str, str], list[DetectionCandidate]]


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
