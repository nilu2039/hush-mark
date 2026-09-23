from bisect import bisect_left
from collections.abc import Callable
from math import ceil, floor, isfinite
from typing import BinaryIO
from uuid import uuid4

from openai import OpenAIError

from hushmark_api.overlap import resolve_overlaps
from hushmark_api.recognizers import (
    DetectionCandidate,
    detect_contextual_pii,
    detect_openai_person_pii,
    detect_structured_pii,
)
from hushmark_api.schemas import (
    MAX_TEXT_LENGTH,
    AnalyzeAudioResponseV1,
    AnalyzeResponseV1,
    AudioDetectionV1,
    DetectionV1,
)
from hushmark_api.transcription import TimedTranscript

ContextDetector = Callable[[str, str], list[DetectionCandidate]]
PersonDetector = Callable[[str, str], list[DetectionCandidate]]
AudioTranscriber = Callable[[BinaryIO, str, str], TimedTranscript]


class NoSpeechDetectedError(Exception):
    pass


class TranscriptTooLargeError(Exception):
    pass


class ContextualAnalysisUnavailableError(Exception):
    pass


class TimingUnavailableError(Exception):
    pass


def analyze_document(
    text: str,
    locale: str,
    contextual_detector: ContextDetector = detect_contextual_pii,
    person_detector: PersonDetector | None = None,
) -> AnalyzeResponseV1:
    candidates = detect_structured_pii(text)
    candidates.extend(contextual_detector(text, locale))
    try:
        candidates.extend((person_detector or detect_openai_person_pii)(text, locale))
    except OpenAIError as error:
        raise ContextualAnalysisUnavailableError from error
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
    transcription = transcriber(audio, filename, content_type)
    transcript = transcription.text
    if not transcript.strip():
        raise NoSpeechDetectedError
    if len(transcript) > MAX_TEXT_LENGTH:
        raise TranscriptTooLargeError

    analysis = analyze_document(transcript, "en-IN")
    timings = _word_timings(transcription)
    offsets = [offset for offset, _ in timings]
    word_indexes = [word_index for _, word_index in timings]
    detections: list[AudioDetectionV1] = []
    for detection in analysis.detections:
        first_index = bisect_left(offsets, detection.start)
        last_index = bisect_left(offsets, detection.end) - 1
        if first_index > last_index:
            raise TimingUnavailableError
        first_word = transcription.words[word_indexes[first_index]]
        last_word = transcription.words[word_indexes[last_index]]
        detections.append(
            AudioDetectionV1(
                **detection.model_dump(by_alias=True),
                audioStartMs=floor(first_word.start * 1000),
                audioEndMs=ceil(last_word.end * 1000),
            )
        )
    return AnalyzeAudioResponseV1(
        transcript=transcript,
        analysis_id=analysis.analysis_id,
        text_length=analysis.text_length,
        detections=detections,
    )


def _word_timings(transcription: TimedTranscript) -> list[tuple[int, int]]:
    transcript_chars = [
        (character.casefold(), offset)
        for offset, character in enumerate(transcription.text)
        if character.isalnum()
    ]
    word_chars: list[tuple[str, int]] = []
    previous_start = 0.0
    previous_end = 0.0
    for index, word in enumerate(transcription.words):
        if (
            not isfinite(word.start)
            or not isfinite(word.end)
            or word.start < previous_start
            or word.end < previous_end
            or word.end <= word.start
        ):
            raise TimingUnavailableError
        previous_start = word.start
        previous_end = word.end
        word_chars.extend(
            (character.casefold(), index)
            for character in word.text
            if character.isalnum()
        )
    if [character for character, _ in transcript_chars] != [
        character for character, _ in word_chars
    ]:
        raise TimingUnavailableError
    return [
        (offset, word_index)
        for (_, offset), (_, word_index) in zip(transcript_chars, word_chars)
    ]
