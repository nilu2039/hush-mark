import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from openai import OpenAIError
from pydantic import ValidationError

from hushmark_api.main import app, main
from hushmark_api.overlap import resolve_overlaps
from hushmark_api.recognizers import DetectionCandidate, detect_structured_pii
from hushmark_api.schemas import (
    MAX_TEXT_LENGTH,
    AnalyzeRequestV1,
    DetectionSource,
    EntityType,
)
from hushmark_api.service import analyze_document
from hushmark_api.transcription import transcribe_audio

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
        response = analyze_document(text, "en-IN", lambda _text, _locale: [])

        self.assertTrue(response.analysis_id.startswith("ana_"))
        self.assertEqual([item.id for item in response.detections], ["det_1", "det_2"])
        self.assertEqual(response.text_length, len(text))

    def test_contextual_person_and_address_detection(self) -> None:
        text = "Aarav Sharma lives at 42 Lake View Road, Bengaluru."
        response = analyze_document(text, "en-IN")
        detected = {
            (item.entity_type, text[item.start : item.end])
            for item in response.detections
        }

        self.assertIn((EntityType.PERSON, "Aarav Sharma"), detected)
        self.assertIn((EntityType.ADDRESS, "42 Lake View Road"), detected)

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
        self.assertNotIn("/anonymize", schema["paths"])

    @patch("hushmark_api.main.transcribe_audio")
    def test_audio_upload_returns_transcript_and_detections(
        self, transcriber: MagicMock
    ) -> None:
        transcript = "Email sample@example.com."
        transcriber.return_value = transcript

        response = client.post(
            "/v1/analyze/audio",
            files={"file": ("recording.wav", b"synthetic audio", "audio/wav")},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["transcript"], transcript)
        email = next(item for item in body["detections"] if item["type"] == "EMAIL")
        self.assertEqual(transcript[email["start"] : email["end"]], "sample@example.com")
        _, forwarded_name, forwarded_type = transcriber.call_args.args
        self.assertEqual(forwarded_name, "audio.wav")
        self.assertEqual(forwarded_type, "audio/wav")

    @patch("hushmark_api.transcription._client")
    def test_transcriber_uses_model_and_language_hints(
        self, openai_client: MagicMock
    ) -> None:
        sdk = openai_client.return_value
        sdk.audio.transcriptions.create.return_value.text = "Synthetic transcript."
        audio = BytesIO(b"synthetic audio")

        transcript = transcribe_audio(audio, "audio.wav", "audio/wav")

        self.assertEqual(transcript, "Synthetic transcript.")
        sdk.audio.transcriptions.create.assert_called_once_with(
            model="gpt-transcribe",
            file=("audio.wav", audio, "audio/wav"),
            languages=["en", "hi"],
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
            ("   ", 422, "no_speech_detected"),
            ("x" * (MAX_TEXT_LENGTH + 1), 422, "transcript_too_large"),
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

    @patch("uvicorn.run")
    def test_cli_runs_the_api(self, run: MagicMock) -> None:
        main()

        run.assert_called_once_with(
            "hushmark_api.main:app", host="127.0.0.1", port=8000
        )


if __name__ == "__main__":
    unittest.main()
