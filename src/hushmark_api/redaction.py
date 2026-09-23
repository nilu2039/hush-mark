import json
import shutil
import subprocess
from math import isfinite
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO

from hushmark_api.schemas import RedactAudioReviewV1, ReviewStatus

_PROCESS_TIMEOUT_SECONDS = 120
_PADDING_SECONDS = 0.1


class InvalidAudioError(Exception):
    pass


class InvalidReviewError(Exception):
    pass


class AudioProcessingUnavailableError(Exception):
    pass


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_PROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AudioProcessingUnavailableError from error


def _duration(path: Path) -> float:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=duration:format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    if result.returncode:
        raise InvalidAudioError
    try:
        probe = json.loads(result.stdout)
        if not probe.get("streams"):
            raise ValueError
        raw_duration = probe["streams"][0].get("duration")
        if raw_duration in (None, "N/A"):
            raw_duration = probe["format"]["duration"]
        duration = float(raw_duration)
        if not isfinite(duration) or duration <= 0:
            raise ValueError
    except (ValueError, TypeError, KeyError, IndexError) as error:
        raise InvalidAudioError from error
    return duration


def _approved_intervals(
    review: RedactAudioReviewV1, duration: float
) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    for detection in review.detections:
        start = detection.audio_start_ms / 1000
        end = detection.audio_end_ms / 1000
        if end > duration + 0.001:
            raise InvalidReviewError
        if detection.status == ReviewStatus.APPROVED:
            intervals.append(
                (
                    max(0, start - _PADDING_SECONDS),
                    min(duration, end + _PADDING_SECONDS),
                )
            )
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _filter_graph(intervals: list[tuple[float, float]], duration: float) -> str:
    active = "+".join(
        f"between(t\\,{start:.3f}\\,{end:.3f})" for start, end in intervals
    )
    return (
        "[0:a:0]aresample=48000,asetnsamples=n=48:p=1,"
        f"volume='1-clip({active}\\,0\\,1)':eval=frame[original];"
        f"sine=frequency=1000:sample_rate=48000:duration={duration:.3f},"
        "asetnsamples=n=48:p=1,"
        f"volume='2*clip({active}\\,0\\,1)':eval=frame[beep];"
        "[original][beep]amix=inputs=2:duration=first:normalize=0[out]"
    )


def render_redacted_audio(
    audio: BinaryIO, suffix: str, review: RedactAudioReviewV1
) -> tuple[Path, TemporaryDirectory[str]]:
    temporary_directory = TemporaryDirectory(prefix="hushmark-audio-")
    try:
        source = Path(temporary_directory.name) / f"input.{suffix}"
        output = Path(temporary_directory.name) / "redacted.mp3"
        with source.open("wb") as destination:
            shutil.copyfileobj(audio, destination)
        duration = _duration(source)
        intervals = _approved_intervals(review, duration)
        command = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
        ]
        if intervals:
            command.extend(
                ["-filter_complex", _filter_graph(intervals, duration), "-map", "[out]"]
            )
        else:
            command.extend(["-map", "0:a:0"])
        command.extend(
            ["-vn", "-c:a", "libmp3lame", "-b:a", "128k", str(output)]
        )
        if _run(command).returncode or not output.is_file() or not output.stat().st_size:
            raise AudioProcessingUnavailableError
        return output, temporary_directory
    except Exception:
        temporary_directory.cleanup()
        raise
