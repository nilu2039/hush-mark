from dataclasses import dataclass
from functools import lru_cache
from typing import BinaryIO

from openai import OpenAI

MAX_AUDIO_SIZE_BYTES = 25 * 1024 * 1024
SUPPORTED_AUDIO_EXTENSIONS = frozenset(
    {"flac", "m4a", "mp3", "mp4", "mpeg", "mpga", "ogg", "wav", "webm"}
)


@dataclass(frozen=True, slots=True)
class TimedWord:
    text: str
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class TimedTranscript:
    text: str
    words: tuple[TimedWord, ...]


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    return OpenAI()


def transcribe_audio(
    audio: BinaryIO, filename: str, content_type: str
) -> TimedTranscript:
    transcription = _client().audio.transcriptions.create(
        model="whisper-1",
        file=(filename, audio, content_type),
        response_format="verbose_json",
        timestamp_granularities=["word"],
    )
    words = [
        TimedWord(word.word, word.start, word.end)
        for word in transcription.words or ()
    ]
    for index, word in enumerate(words):
        if word.start != word.end or len(words) == 1:
            continue
        previous = words[index - 1] if index else None
        following = words[index + 1] if index + 1 < len(words) else None
        start = previous.start if previous else word.start
        end = following.end if following else previous.end
        if end > start:
            # Include neighboring speech when the provider gives a word no duration.
            words[index] = TimedWord(word.text, start, end)
    return TimedTranscript(text=transcription.text, words=tuple(words))
