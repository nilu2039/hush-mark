from functools import lru_cache
from typing import BinaryIO

from openai import OpenAI

MAX_AUDIO_SIZE_BYTES = 25 * 1024 * 1024
SUPPORTED_AUDIO_EXTENSIONS = frozenset(
    {"flac", "m4a", "mp3", "mp4", "mpeg", "mpga", "ogg", "wav", "webm"}
)


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    return OpenAI()


def transcribe_audio(audio: BinaryIO, filename: str, content_type: str) -> str:
    transcription = _client().audio.transcriptions.create(
        model="gpt-transcribe",
        file=(filename, audio, content_type),
        languages=["en", "hi"],
    )
    return transcription.text
