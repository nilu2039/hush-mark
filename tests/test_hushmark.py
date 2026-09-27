import json
import math
import shutil
import struct
import subprocess
import unittest
import wave
from array import array
from io import BytesIO
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from openai import OpenAIError
from pydantic import ValidationError

from hushmark_api.main import app, main
from hushmark_api.overlap import resolve_overlaps
from hushmark_api.recognizers import (
    DetectionCandidate,
    detect_openai_person_pii,
    detect_structured_pii,
)
from hushmark_api.schemas import (
    MAX_TEXT_LENGTH,
    AnalyzeRequestV1,
    AnalyzeResponseV1,
    DetectionV1,
    DetectionSource,
    EntityType,
    RedactAudioReviewV1,
)
from hushmark_api.redaction import _approved_intervals
from hushmark_api.service import (
    TimingUnavailableError,
    analyze_audio_document,
    analyze_document,
)
from hushmark_api.transcription import TimedTranscript, TimedWord, transcribe_audio

client = TestClient(app)


class HushMarkTests(unittest.TestCase):
    def test_structured_recognizers_return_end_exclusive_offsets(self) -> None:
        text = (
            "Email jane@example.com, phone +91 98765 43210, PAN ABCDE1234F, "
            "Aadhaar 2345 6789 0124, card 4111 1111 1111 1111, IP 192.0.2.1."
        )
        detections = detect_structured_pii(text)
        detected = {
            (item.entity_type, text[item.start : item.end]) for item in detections
        }

        self.assertIn((EntityType.EMAIL, "jane@example.com"), detected)
        self.assertIn((EntityType.PHONE, "+91 98765 43210"), detected)
        self.assertIn((EntityType.PAN, "ABCDE1234F"), detected)
        self.assertIn((EntityType.AADHAAR, "2345 6789 0124"), detected)
        self.assertIn((EntityType.PAYMENT_CARD, "4111 1111 1111 1111"), detected)
        self.assertIn((EntityType.IP_ADDRESS, "192.0.2.1"), detected)

    def test_invalid_checksums_are_rejected(self) -> None:
        detections = detect_structured_pii(
            "Aadhaar 2345 6789 0123, cards 4111 1111 1111 1112 and "
            "0000 0000 0000 0000"
        )
        types = {item.entity_type for item in detections}

        self.assertNotIn(EntityType.AADHAAR, types)
        self.assertNotIn(EntityType.PAYMENT_CARD, types)

    def test_spoken_indian_phone_returns_original_word_offsets(self) -> None:
        spoken_phone = (
            "plus nine one six zero zero zero zero zero zero zero zero zero"
        )
        text = f"Call {spoken_phone} for a synthetic example."

        detections = detect_structured_pii(text)
        phone = next(item for item in detections if item.entity_type == EntityType.PHONE)

        self.assertEqual(text[phone.start : phone.end], spoken_phone)
        self.assertFalse(
            detect_structured_pii(
                "Too short: six zero zero zero zero zero zero zero zero."
            )
        )

    def test_structured_detection_wins_an_overlap(self) -> None:
        contextual = DetectionCandidate(
            EntityType.ADDRESS, 0, 16, 0.99, DetectionSource.PRESIDIO
        )
        structured = DetectionCandidate(
            EntityType.EMAIL, 4, 16, 0.8, DetectionSource.REGEX
        )

        self.assertEqual(resolve_overlaps([contextual, structured]), [structured])

    def test_analysis_ids_and_detection_ids_have_stable_shapes(self) -> None:
        text = "jane@example.com and jane@example.com"
        response = analyze_document(
            text,
            "en-IN",
            lambda _text, _locale: [],
            lambda _text, _locale: [],
        )

        self.assertTrue(response.analysis_id.startswith("ana_"))
        self.assertEqual([item.id for item in response.detections], ["det_1", "det_2"])
        self.assertEqual(response.text_length, len(text))

    def test_contextual_person_and_address_detection(self) -> None:
        text = "Aarav Sharma lives at 42 Lake View Road, Bengaluru."
        response = analyze_document(
            text, "en-IN", person_detector=lambda _text, _locale: []
        )
        detected = {
            (item.entity_type, text[item.start : item.end])
            for item in response.detections
        }

        self.assertIn((EntityType.PERSON, "Aarav Sharma"), detected)
        self.assertIn((EntityType.ADDRESS, "42 Lake View Road, Bengaluru"), detected)

    @patch("hushmark_api.recognizers._openai_client")
    def test_openai_person_detection_uses_exact_local_offsets(
        self, openai_client: MagicMock
    ) -> None:
        text = "Aarav Sen called Aarav Sen."
        openai_client.return_value.responses.parse.return_value.output_parsed.names = [
            "Aarav Sen",
            "not present",
        ]

        detections = detect_openai_person_pii(text, "en-IN")

        self.assertEqual(
            [(item.start, item.end, item.source) for item in detections],
            [
                (0, 9, DetectionSource.OPENAI),
                (17, 26, DetectionSource.OPENAI),
            ],
        )
        request = openai_client.return_value.responses.parse.call_args.kwargs
        self.assertEqual(request["model"], "gpt-5.4-nano")
        self.assertEqual(request["input"][1]["content"], text)
        self.assertFalse(request["store"])

    def test_request_rejects_empty_oversized_and_extra_fields(self) -> None:
        invalid_requests = (
            {"text": ""},
            {"text": " "},
            {"text": "x" * (MAX_TEXT_LENGTH + 1)},
            {"text": "hello", "unknown": True},
        )
        for request in invalid_requests:
            with self.subTest(request_length=len(request["text"])):
                with self.assertRaises(ValidationError):
                    AnalyzeRequestV1.model_validate(request)

    def test_openapi_exposes_versioned_analysis_routes(self) -> None:
        schema = app.openapi()

        self.assertIn("/v1/analyze", schema["paths"])
        self.assertIn("/v1/analyze/audio", schema["paths"])
        self.assertIn("/v1/redact/audio", schema["paths"])
        self.assertNotIn("/anonymize", schema["paths"])
        self.assertEqual(
            list(
                schema["paths"]["/v1/redact/audio"]["post"]["responses"]["200"][
                    "content"
                ]
            ),
            ["audio/mpeg"],
        )

    @patch("hushmark_api.main.transcribe_audio")
    def test_audio_upload_returns_transcript_and_detections(
        self, transcriber: MagicMock
    ) -> None:
        transcript = "Email sample@example.com."
        transcriber.return_value = TimedTranscript(
            transcript,
            (
                TimedWord("Email", 0.0, 0.3),
                TimedWord("sample@example.com.", 0.4, 1.2),
            ),
        )

        with patch(
            "hushmark_api.service.detect_openai_person_pii", return_value=[]
        ):
            response = client.post(
                "/v1/analyze/audio",
                files={"file": ("recording.wav", b"synthetic audio", "audio/wav")},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["transcript"], transcript)
        self.assertEqual(body["wordTimings"], [
            {"start": 0, "end": 5, "audioStartMs": 0, "audioEndMs": 300},
            {"start": 6, "end": 24, "audioStartMs": 400, "audioEndMs": 1200},
        ])
        email = next(item for item in body["detections"] if item["type"] == "EMAIL")
        self.assertEqual(transcript[email["start"] : email["end"]], "sample@example.com")
        self.assertEqual((email["audioStartMs"], email["audioEndMs"]), (400, 1200))
        _, forwarded_name, forwarded_type = transcriber.call_args.args
        self.assertEqual(forwarded_name, "audio.wav")
        self.assertEqual(forwarded_type, "audio/wav")

    @patch(
        "hushmark_api.service.detect_openai_person_pii",
        side_effect=OpenAIError("provider detail"),
    )
    def test_text_analysis_returns_safe_contextual_errors(
        self, _person_detector: MagicMock
    ) -> None:
        response = client.post(
            "/v1/analyze", json={"text": "Synthetic text.", "locale": "en-IN"}
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "analysis_unavailable")
        self.assertNotIn("provider detail", response.text)

    @patch("hushmark_api.transcription._client")
    def test_transcriber_returns_timestamped_words(
        self, openai_client: MagicMock
    ) -> None:
        sdk = openai_client.return_value
        sdk.audio.transcriptions.create.return_value.text = "Synthetic transcript."
        sdk.audio.transcriptions.create.return_value.words = [
            MagicMock(word="Synthetic", start=0.1, end=0.5),
            MagicMock(word="transcript.", start=0.6, end=1.0),
        ]
        audio = BytesIO(b"synthetic audio")

        transcript = transcribe_audio(audio, "audio.wav", "audio/wav")

        self.assertEqual(transcript.text, "Synthetic transcript.")
        self.assertEqual(transcript.words[1], TimedWord("transcript.", 0.6, 1.0))
        sdk.audio.transcriptions.create.assert_called_once_with(
            model="whisper-1",
            file=("audio.wav", audio, "audio/wav"),
            response_format="verbose_json",
            timestamp_granularities=["word"],
        )

    @patch("hushmark_api.main.transcribe_audio")
    def test_audio_upload_rejects_invalid_inputs(self, transcriber: MagicMock) -> None:
        missing = client.post("/v1/analyze/audio")
        self.assertEqual(missing.status_code, 422)
        self.assertEqual(missing.json()["code"], "invalid_audio")

        cases = (
            (("recording.txt", b"audio", "text/plain"), 415, "unsupported_audio_type"),
            (("recording.wav", b"", "audio/wav"), 422, "invalid_audio"),
        )
        for uploaded_file, status, code in cases:
            with self.subTest(code=code):
                response = client.post(
                    "/v1/analyze/audio", files={"file": uploaded_file}
                )
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["code"], code)

        transcriber.assert_not_called()

    @patch("hushmark_api.main.MAX_AUDIO_SIZE_BYTES", 3)
    @patch("hushmark_api.main.transcribe_audio")
    def test_audio_upload_rejects_oversized_files(
        self, transcriber: MagicMock
    ) -> None:
        response = client.post(
            "/v1/analyze/audio",
            files={"file": ("recording.wav", b"four", "audio/wav")},
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["code"], "audio_too_large")
        transcriber.assert_not_called()

    @patch("hushmark_api.main.transcribe_audio")
    def test_audio_upload_returns_safe_transcription_errors(
        self, transcriber: MagicMock
    ) -> None:
        cases = (
            (TimedTranscript("   ", ()), 422, "no_speech_detected"),
            (
                TimedTranscript("x" * (MAX_TEXT_LENGTH + 1), ()),
                422,
                "transcript_too_large",
            ),
            (OpenAIError("provider detail"), 503, "transcription_unavailable"),
        )
        for result, status, code in cases:
            with self.subTest(code=code):
                if isinstance(result, Exception):
                    transcriber.side_effect = result
                    transcriber.return_value = None
                else:
                    transcriber.side_effect = None
                    transcriber.return_value = result

                response = client.post(
                    "/v1/analyze/audio",
                    files={"file": ("recording.wav", b"audio", "audio/wav")},
                )

                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["code"], code)
                self.assertNotIn("provider detail", response.text)

    def test_audio_alignment_rejects_mismatched_words(self) -> None:
        with patch("hushmark_api.service.analyze_document") as analyze:
            analyze.return_value.detections = []
            analyze.return_value.analysis_id = "ana_" + "0" * 32
            analyze.return_value.text_length = 19
            with self.assertRaises(TimingUnavailableError):
                analyze_audio_document(
                    BytesIO(b"synthetic audio"),
                    "audio.wav",
                    "audio/wav",
                    lambda *_: TimedTranscript(
                        "Email a@example.com", (TimedWord("different", 0, 1),)
                    ),
                )

    @patch("hushmark_api.service.analyze_document")
    def test_audio_alignment_covers_multiword_and_punctuation(
        self, analyze: MagicMock
    ) -> None:
        transcript = "Aarav Sen: sample@example.com!"
        analyze.return_value = AnalyzeResponseV1(
            analysisId="ana_" + "0" * 32,
            textLength=len(transcript),
            detections=[
                DetectionV1(
                    id="det_1",
                    type="PERSON",
                    start=0,
                    end=9,
                    confidence=0.85,
                    source="openai",
                ),
                DetectionV1(
                    id="det_2",
                    type="EMAIL",
                    start=11,
                    end=29,
                    confidence=0.99,
                    source="regex",
                ),
            ],
        )
        transcription = TimedTranscript(
            transcript,
            (
                TimedWord("Aarav", 0.1, 0.4),
                TimedWord("Sen:", 0.39, 0.7),
                TimedWord("sample@example.com!", 0.8, 1.5),
            ),
        )

        response = analyze_audio_document(
            BytesIO(b"synthetic audio"),
            "audio.wav",
            "audio/wav",
            lambda *_: transcription,
        )

        self.assertEqual(
            [(item.audio_start_ms, item.audio_end_ms) for item in response.detections],
            [(100, 700), (800, 1500)],
        )
        self.assertEqual(
            [(item.start, item.end, item.audio_start_ms, item.audio_end_ms)
             for item in response.word_timings],
            [(0, 5, 100, 400), (6, 9, 390, 700), (11, 29, 800, 1500)],
        )

    @patch("hushmark_api.service.analyze_document")
    def test_audio_word_spans_use_unicode_character_offsets(
        self, analyze: MagicMock
    ) -> None:
        transcript = "👋 Aarav Sen!"
        analyze.return_value = AnalyzeResponseV1(
            analysisId="ana_" + "0" * 32,
            textLength=len(transcript),
            detections=[],
        )
        response = analyze_audio_document(
            BytesIO(b"synthetic audio"),
            "audio.wav",
            "audio/wav",
            lambda *_: TimedTranscript(
                transcript,
                (TimedWord("Aarav", 0.1, 0.4), TimedWord("Sen!", 0.5, 0.8)),
            ),
        )
        self.assertEqual(
            [(item.start, item.end) for item in response.word_timings],
            [(2, 7), (8, 11)],
        )

    @staticmethod
    def _synthetic_wav() -> bytes:
        output = BytesIO()
        with wave.open(output, "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(8000)
            recording.writeframes(
                b"".join(
                    struct.pack(
                        "<h",
                        round(10000 * math.sin(2 * math.pi * 440 * sample / 8000)),
                    )
                    for sample in range(16000)
                )
            )
        return output.getvalue()

    @staticmethod
    def _review(status: str = "approved", start: int = 500, end: int = 1000) -> str:
        return json.dumps(
            {
                "analysisId": "ana_" + "0" * 32,
                "detections": [
                    {
                        "id": "det_1",
                        "status": status,
                        "audioStartMs": start,
                        "audioEndMs": end,
                    }
                ],
            }
        )

    def test_approved_intervals_merge_after_padding(self) -> None:
        review = RedactAudioReviewV1.model_validate(
            {
                "analysisId": "ana_" + "0" * 32,
                "detections": [
                    {
                        "id": "det_1", "status": "approved",
                        "audioStartMs": 0, "audioEndMs": 100,
                    },
                    {
                        "id": "det_2", "status": "approved",
                        "audioStartMs": 250, "audioEndMs": 400,
                    },
                    {
                        "id": "det_3", "status": "rejected",
                        "audioStartMs": 500, "audioEndMs": 600,
                    },
                ],
            }
        )
        self.assertEqual(_approved_intervals(review, 0.7), [(0, 0.5)])

    def test_audio_review_accepts_typed_manual_and_legacy_automatic_marks(self) -> None:
        review = RedactAudioReviewV1.model_validate(
            {
                "analysisId": "ana_" + "0" * 32,
                "detections": [
                    {
                        "id": "man_1", "type": "EMAIL", "status": "approved",
                        "audioStartMs": 500, "audioEndMs": 1000,
                    },
                    {
                        "id": "det_2", "type": "PERSON", "status": "rejected",
                        "audioStartMs": 300, "audioEndMs": 600,
                    },
                    {
                        "id": "det_1", "status": "rejected",
                        "audioStartMs": 0, "audioEndMs": 100,
                    },
                ],
            }
        )
        self.assertEqual(_approved_intervals(review, 2.0), [(0.4, 1.1)])
        self.assertIsNone(review.detections[2].entity_type)

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required"
    )
    def test_audio_redaction_replaces_original_speech_with_beep(self) -> None:
        response = client.post(
            "/v1/redact/audio",
            data={"review": self._review()},
            files={"file": ("recording.wav", self._synthetic_wav(), "audio/wav")},
        )

        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertEqual(response.headers["content-type"], "audio/mpeg")
        self.assertEqual(response.headers["cache-control"], "no-store")
        decoded = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "f32le",
                "-ac", "1", "-ar", "8000", "pipe:1",
            ],
            input=response.content,
            capture_output=True,
            check=True,
        )
        samples = array("f")
        samples.frombytes(decoded.stdout)

        def strength(start: float, end: float, frequency: int) -> float:
            chunk = samples[round(start * 8000) : round(end * 8000)]
            real = sum(
                value * math.cos(2 * math.pi * frequency * index / 8000)
                for index, value in enumerate(chunk)
            )
            imaginary = sum(
                value * math.sin(2 * math.pi * frequency * index / 8000)
                for index, value in enumerate(chunk)
            )
            return math.hypot(real, imaginary) / len(chunk)

        self.assertGreater(strength(0.1, 0.3, 440), 0.05)
        self.assertLess(strength(0.6, 0.9, 440), 0.005)
        self.assertGreater(strength(0.6, 0.9, 1000), 0.05)

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required"
    )
    def test_rejected_detection_keeps_original_audio(self) -> None:
        response = client.post(
            "/v1/redact/audio",
            data={"review": self._review(status="rejected")},
            files={"file": ("recording.wav", self._synthetic_wav(), "audio/wav")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"ID3"))

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required"
    )
    def test_manual_audio_mark_exports_beep(self) -> None:
        review = {
            "analysisId": "ana_" + "0" * 32,
            "detections": [
                {
                    "id": "man_1", "type": "EMAIL", "status": "approved",
                    "audioStartMs": 500, "audioEndMs": 1000,
                }
            ],
        }
        response = client.post(
            "/v1/redact/audio",
            data={"review": json.dumps(review)},
            files={"file": ("recording.wav", self._synthetic_wav(), "audio/wav")},
        )
        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertEqual(response.headers["content-type"], "audio/mpeg")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertTrue(response.content.startswith(b"ID3"))

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required"
    )
    def test_redaction_accepts_separate_beep_intervals(self) -> None:
        review = json.loads(self._review(start=300, end=500))
        review["detections"].append(
            {
                "id": "det_2",
                "status": "approved",
                "audioStartMs": 1300,
                "audioEndMs": 1500,
            }
        )
        response = client.post(
            "/v1/redact/audio",
            data={"review": json.dumps(review)},
            files={"file": ("recording.wav", self._synthetic_wav(), "audio/wav")},
        )
        self.assertEqual(response.status_code, 200, response.text[:200])

    def test_redaction_rejects_pending_invalid_and_out_of_duration_review(self) -> None:
        duplicate_review = json.loads(self._review())
        duplicate_review["detections"].append(duplicate_review["detections"][0])
        extra_and_pending = json.loads(self._review(status="pending"))
        extra_and_pending["detections"][0]["extra"] = "test_private_marker"
        manual_missing_type = json.loads(self._review())
        manual_missing_type["detections"][0]["id"] = "man_1"
        manual_invalid_type = json.loads(self._review())
        manual_invalid_type["detections"][0].update(id="man_1", type="test_private_marker")
        manual_out_of_duration = json.loads(self._review(start=500, end=3000))
        manual_out_of_duration["detections"][0].update(id="man_1", type="EMAIL")
        cases = (
            (self._review(status="pending"), ("Set every detection status",)),
            (self._review(start=1000, end=500), ("0 <= audioStartMs < audioEndMs",)),
            (self._review(start=500, end=3000), ("recording's duration",)),
            (json.dumps(manual_out_of_duration), ("recording's duration",)),
            (json.dumps(duplicate_review), ("no gaps or duplicates",)),
            (json.dumps(manual_missing_type), ("valid PII type",)),
            (json.dumps(manual_invalid_type), ("valid PII type",)),
            (
                json.dumps(extra_and_pending),
                ("Set every detection status", "Remove extra review fields"),
            ),
            (json.dumps(self._review()), ("without quotes around the entire object",)),
            ("not json", ("valid JSON",)),
        )
        for review, expected_messages in cases:
            with self.subTest(expected_messages=expected_messages):
                response = client.post(
                    "/v1/redact/audio",
                    data={"review": review},
                    files={"file": ("recording.wav", self._synthetic_wav(), "audio/wav")},
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "invalid_review")
                for expected_message in expected_messages:
                    self.assertIn(expected_message, response.json()["message"])
                self.assertNotIn("test_private_marker", response.text)

        missing_review = client.post(
            "/v1/redact/audio",
            files={"file": ("recording.wav", self._synthetic_wav(), "audio/wav")},
        )
        self.assertEqual(missing_review.status_code, 422)
        self.assertEqual(missing_review.json()["code"], "invalid_review")
        self.assertIn("Add a review form field", missing_review.json()["message"])

    def test_redaction_rejects_corrupt_audio(self) -> None:
        response = client.post(
            "/v1/redact/audio",
            data={"review": self._review()},
            files={"file": ("recording.wav", b"not audio", "audio/wav")},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "invalid_audio")

    @patch("uvicorn.run")
    def test_cli_runs_the_api(self, run: MagicMock) -> None:
        main()

        run.assert_called_once_with(
            "hushmark_api.main:app", host="127.0.0.1", port=8000
        )


if __name__ == "__main__":
    unittest.main()
