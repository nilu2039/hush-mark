import unittest
from unittest.mock import MagicMock, patch

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

    def test_openapi_exposes_only_the_versioned_analysis_route(self) -> None:
        schema = app.openapi()

        self.assertIn("/v1/analyze", schema["paths"])
        self.assertNotIn("/anonymize", schema["paths"])

    @patch("uvicorn.run")
    def test_cli_runs_the_api(self, run: MagicMock) -> None:
        main()

        run.assert_called_once_with(
            "hushmark_api.main:app", host="127.0.0.1", port=8000
        )


if __name__ == "__main__":
    unittest.main()
