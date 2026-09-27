import json
import unittest
from codecs import BOM_UTF8
from unittest.mock import patch

from fastapi.testclient import TestClient
from openai import OpenAIError

from hushmark_api.documents import MAX_DOCUMENT_SIZE_BYTES
from hushmark_api.main import app
from hushmark_api.schemas import MAX_TEXT_LENGTH

client = TestClient(app)
ANALYSIS_ID = "ana_" + "0" * 32


class DocumentTests(unittest.TestCase):
    @staticmethod
    def _analyze(filename: str, content: bytes):
        with (
            patch("hushmark_api.recognizers._context_analyzer") as analyzer,
            patch("hushmark_api.service.detect_openai_person_pii", return_value=[]),
        ):
            analyzer.return_value.analyze.return_value = []
            return client.post(
                "/v1/analyze/document", files={"file": (filename, content)}
            )

    @staticmethod
    def _redact(filename: str, content: bytes, detections: list[dict]):
        return client.post(
            "/v1/redact/document",
            files={"file": (filename, content)},
            data={
                "review": json.dumps(
                    {"analysisId": ANALYSIS_ID, "detections": detections}
                )
            },
        )

    def test_analysis_returns_decoded_text_and_exact_offsets(self) -> None:
        for filename, content in (
            ("notes.txt", BOM_UTF8 + b"Email sample@example.com.\r\n"),
            ("notes.md", b"# Contact\r\n**Email:** sample@example.com\r\n"),
            ("notes.markdown", b"# Contact\r\n**Email:** sample@example.com\r\n"),
        ):
            with self.subTest(filename=filename):
                response = self._analyze(filename, content)

                self.assertEqual(response.status_code, 200)
                body = response.json()
                text = content.decode("utf-8-sig")
                self.assertEqual(body["text"], text)
                self.assertEqual(body["textLength"], len(text))
                self.assertNotIn("redactedText", body)
                self.assertEqual(len(body["detections"]), 1)
                detection = body["detections"][0]
                start = text.index("sample@example.com")
                self.assertEqual(
                    (detection["start"], detection["end"]), (start, start + 18)
                )
                self.assertEqual(
                    text[detection["start"] : detection["end"]],
                    "sample@example.com",
                )
                self.assertEqual(
                    (detection["id"], detection["type"], detection["status"]),
                    ("det_1", "EMAIL", "pending"),
                )

    def test_reviewed_download_replaces_only_approved_spans(self) -> None:
        text = "**Email:** sample@example.com\r\nSecond: other@example.com\r\n"
        analysis = self._analyze("notes.md", text.encode("utf-8")).json()
        detections = [
            {
                "id": item["id"],
                "type": item["type"],
                "start": item["start"],
                "end": item["end"],
                "status": "approved" if index == 0 else "rejected",
            }
            for index, item in enumerate(analysis["detections"])
        ]

        response = self._redact("notes.md", text.encode("utf-8"), detections)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.content.decode("utf-8"),
            "**Email:** [EMAIL]\r\nSecond: other@example.com\r\n",
        )
        self.assertEqual(response.headers["content-type"], "text/markdown; charset=utf-8")
        self.assertEqual(
            response.headers["content-disposition"], 'attachment; filename="redacted.md"'
        )
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_manual_document_marks_can_be_reordered_and_overlap_rejected_marks(
        self,
    ) -> None:
        content = BOM_UTF8 + b"AA BB CC DD\r\n"
        detections = [
            {"id": "det_2", "type": "EMAIL", "start": 9, "end": 11, "status": "approved"},
            {"id": "det_1", "type": "PERSON", "start": 3, "end": 8, "status": "rejected"},
            {"id": "man_1", "type": "PERSON", "start": 3, "end": 5, "status": "approved"},
        ]

        response = self._redact("notes.md", content, detections)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, BOM_UTF8 + b"AA [PERSON] CC [EMAIL]\r\n")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_pasted_text_redaction_accepts_manual_only_and_edited_marks(self) -> None:
        text = "AA BB CC DD\r\n"
        review = {
            "analysisId": ANALYSIS_ID,
            "detections": [
                {"id": "det_2", "type": "PAN", "start": 9, "end": 11, "status": "rejected"},
                {"id": "man_1", "type": "PERSON", "start": 3, "end": 5, "status": "approved"},
                {"id": "det_1", "type": "EMAIL", "start": 0, "end": 2, "status": "approved"},
            ],
        }
        response = client.post("/v1/redact", json={"text": text, "review": review})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "[EMAIL] [PERSON] CC DD\r\n")
        self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(response.headers["cache-control"], "no-store")

        manual_only = client.post(
            "/v1/redact",
            json={
                "text": "AA BB",
                "review": {
                    "analysisId": ANALYSIS_ID,
                    "detections": [
                        {
                            "id": "man_1", "type": "PERSON", "start": 3,
                            "end": 5, "status": "approved",
                        }
                    ],
                },
            },
        )
        self.assertEqual(manual_only.status_code, 200)
        self.assertEqual(manual_only.text, "AA [PERSON]")

    def test_pasted_text_accepts_edited_automatic_type_and_range(self) -> None:
        text = "Email sample@example.com."
        analyzed = self._analyze("notes.txt", text.encode("utf-8")).json()
        detection = analyzed["detections"][0]
        self.assertEqual((detection["start"], detection["end"]), (6, 24))
        detection.update(type="PERSON", end=12, status="approved")

        response = client.post(
            "/v1/redact",
            json={
                "text": text,
                "review": {
                    "analysisId": analyzed["analysisId"],
                    "detections": [
                        {
                            key: detection[key]
                            for key in ("id", "type", "start", "end", "status")
                        }
                    ],
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "Email [PERSON]@example.com.")

    def test_adjacent_spans_and_bom_are_preserved(self) -> None:
        content = BOM_UTF8 + "AB\r\n".encode("utf-8")
        detections = [
            {
                "id": "det_1", "type": "PERSON", "start": 0, "end": 1,
                "status": "approved",
            },
            {
                "id": "det_2", "type": "EMAIL", "start": 1, "end": 2,
                "status": "approved",
            },
        ]

        response = self._redact("notes.markdown", content, detections)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, BOM_UTF8 + b"[PERSON][EMAIL]\r\n")
        self.assertEqual(
            response.headers["content-disposition"],
            'attachment; filename="redacted.markdown"',
        )

    def test_no_approved_spans_leaves_text_unchanged(self) -> None:
        content = b"No change: sample@example.com\r\n"
        for detections in (
            [],
            [{
                "id": "det_1", "type": "EMAIL", "start": 11, "end": 29,
                "status": "rejected",
            }],
        ):
            with self.subTest(detections=detections):
                response = self._redact("notes.txt", content, detections)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, content)
                self.assertEqual(
                    response.headers["content-type"], "text/plain; charset=utf-8"
                )

        uppercase = self._redact("NOTES.TXT", content, [])
        self.assertEqual(uppercase.status_code, 200)
        self.assertEqual(
            uppercase.headers["content-disposition"],
            'attachment; filename="redacted.TXT"',
        )

    def test_analysis_rejects_invalid_uploads_without_echoing_input(self) -> None:
        cases = (
            ("notes.docx", b"private_marker", 415, "unsupported_text_type"),
            ("notes.txt", b"", 422, "invalid_text_file"),
            ("notes.txt", b" \r\n", 422, "invalid_text_file"),
            ("notes.txt", b"private_marker\xff", 422, "invalid_text_file"),
            (
                "notes.txt", b"x" * (MAX_DOCUMENT_SIZE_BYTES + 1),
                413, "text_file_too_large",
            ),
            ("notes.txt", b"x" * (MAX_TEXT_LENGTH + 1), 422, "text_too_large"),
        )
        for filename, content, status, code in cases:
            with self.subTest(filename=filename, code=code):
                response = self._analyze(filename, content)
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
                self.assertEqual(set(response.json()), {"code", "message"})
                self.assertNotIn("private_marker", response.text)
                self.assertNotIn(filename, response.text)

        missing = client.post("/v1/analyze/document")
        self.assertEqual(missing.status_code, 422)
        self.assertEqual(missing.json()["code"], "invalid_text_file")

    def test_analysis_returns_safe_detector_failure(self) -> None:
        with (
            patch("hushmark_api.recognizers._context_analyzer") as analyzer,
            patch(
                "hushmark_api.service.detect_openai_person_pii",
                side_effect=OpenAIError("private_provider_detail"),
            ),
        ):
            analyzer.return_value.analyze.return_value = []
            response = client.post(
                "/v1/analyze/document",
                files={"file": ("notes.txt", b"Synthetic text.")},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "analysis_unavailable")
        self.assertNotIn("private_provider_detail", response.text)

    @patch("hushmark_api.service.detect_openai_person_pii", return_value=[])
    def test_address_export_covers_street_city_and_pin(self, _person_detector) -> None:
        text = (
            "Mailing address: 42 Example Road, Bengaluru 560000. "
            "Email: sample@example.com.\n"
        )
        content = text.encode("utf-8")
        analysis_response = client.post(
            "/v1/analyze/document", files={"file": ("notes.md", content)}
        )

        self.assertEqual(analysis_response.status_code, 200)
        analysis = analysis_response.json()
        address = next(
            item for item in analysis["detections"] if item["type"] == "ADDRESS"
        )
        expected = "42 Example Road, Bengaluru 560000"
        start = text.index(expected)
        self.assertEqual(
            (address["start"], address["end"]), (start, start + len(expected))
        )
        review = {
            "analysisId": analysis["analysisId"],
            "detections": [
                {
                    "id": item["id"],
                    "type": item["type"],
                    "start": item["start"],
                    "end": item["end"],
                    "status": "approved" if item["type"] == "ADDRESS" else "rejected",
                }
                for item in analysis["detections"]
            ],
        }
        export = client.post(
            "/v1/redact/document",
            files={"file": ("notes.md", content)},
            data={"review": json.dumps(review)},
        )

        self.assertEqual(export.status_code, 200)
        self.assertEqual(
            export.content.decode("utf-8"),
            "Mailing address: [ADDRESS]. Email: sample@example.com.\n",
        )

    def test_redaction_rejects_invalid_reviews_and_out_of_range_spans(self) -> None:
        valid = {
            "id": "det_1", "type": "PERSON", "start": 0, "end": 2,
            "status": "approved",
        }
        cases = (
            ([{**valid, "status": "pending"}], "invalid_review"),
            ([{**valid, "type": "UNKNOWN"}], "invalid_review"),
            ([{**valid, "start": True}], "invalid_review"),
            ([{**valid, "end": 0}], "invalid_review"),
            ([{**valid, "extra": "private_marker"}], "invalid_review"),
            ([{**valid, "id": "det_2"}], "invalid_review"),
            ([valid, valid], "invalid_review"),
            ([valid, {**valid, "id": "det_2", "start": 1, "end": 3}], "invalid_review"),
            ([valid, {**valid, "id": "man_1", "start": 1, "end": 3}], "invalid_review"),
            ([{**valid, "end": 99}], "invalid_review"),
        )
        for detections, code in cases:
            with self.subTest(detections=detections):
                response = self._redact("notes.txt", b"abc", detections)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], code)
                self.assertEqual(set(response.json()), {"code", "message"})
                self.assertNotIn("private_marker", response.text)

        malformed = client.post(
            "/v1/redact/document",
            files={"file": ("notes.txt", b"abc")},
            data={"review": "private_marker"},
        )
        self.assertEqual(malformed.status_code, 422)
        self.assertEqual(malformed.json()["code"], "invalid_review")
        self.assertNotIn("private_marker", malformed.text)

        missing_review = client.post(
            "/v1/redact/document", files={"file": ("notes.txt", b"abc")}
        )
        self.assertEqual(missing_review.status_code, 422)
        self.assertEqual(missing_review.json()["code"], "invalid_review")

        missing_file = client.post(
            "/v1/redact/document",
            data={"review": json.dumps({"analysisId": ANALYSIS_ID, "detections": []})},
        )
        self.assertEqual(missing_file.status_code, 422)
        self.assertEqual(missing_file.json()["code"], "invalid_text_file")

    def test_pasted_text_rejects_invalid_reviews_without_echoing_text(self) -> None:
        valid = {
            "id": "man_1", "type": "PERSON", "start": 0,
            "end": 2, "status": "approved",
        }
        cases = (
            ([valid, {**valid, "id": "man_2", "start": 1, "end": 3}], "invalid_review"),
            ([valid, {**valid, "start": 3, "end": 5}], "invalid_review"),
            ([{**valid, "end": 99}], "invalid_review"),
            ([{**valid, "type": "private_marker"}], "invalid_review"),
            ([{**valid, "status": "pending"}], "invalid_review"),
        )
        for detections, code in cases:
            with self.subTest(code=code, detections=detections):
                response = client.post(
                    "/v1/redact",
                    json={
                        "text": "private_marker",
                        "review": {
                            "analysisId": ANALYSIS_ID,
                            "detections": detections,
                        },
                    },
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], code)
                self.assertEqual(set(response.json()), {"code", "message"})
                self.assertNotIn("private_marker", response.text)

        for payload, code in (
            ({"text": "private_marker"}, "invalid_review"),
            (
                {"text": "  ", "review": {"analysisId": ANALYSIS_ID, "detections": []}},
                "invalid_text",
            ),
            (
                {
                    "text": "x" * (MAX_TEXT_LENGTH + 1),
                    "review": {"analysisId": ANALYSIS_ID, "detections": []},
                },
                "text_too_large",
            ),
        ):
            with self.subTest(code=code):
                response = client.post("/v1/redact", json=payload)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], code)
                self.assertEqual(set(response.json()), {"code", "message"})
                self.assertNotIn("private_marker", response.text)

    def test_redaction_validates_file_too(self) -> None:
        for filename, content, status, code in (
            ("notes.docx", b"abc", 415, "unsupported_text_type"),
            ("notes.txt", b"\xff", 422, "invalid_text_file"),
            (
                "notes.txt", b"x" * (MAX_DOCUMENT_SIZE_BYTES + 1),
                413, "text_file_too_large",
            ),
            ("notes.txt", b"x" * (MAX_TEXT_LENGTH + 1), 422, "text_too_large"),
        ):
            with self.subTest(filename=filename, code=code):
                response = self._redact(filename, content, [])
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))

    def test_openapi_exposes_document_routes(self) -> None:
        schema = app.openapi()
        self.assertIn("/v1/analyze/document", schema["paths"])
        self.assertIn("/v1/redact/document", schema["paths"])
        self.assertIn("/v1/redact", schema["paths"])
        self.assertEqual(
            set(schema["paths"]["/v1/redact"]["post"]["responses"]["200"]["content"]),
            {"text/plain"},
        )
        properties = schema["components"]["schemas"]["AnalyzeDocumentResponseV1"][
            "properties"
        ]
        self.assertIn("text", properties)
        self.assertNotIn("redactedText", properties)
        self.assertEqual(
            set(
                schema["paths"]["/v1/redact/document"]["post"]["responses"]["200"][
                    "content"
                ]
            ),
            {"text/plain", "text/markdown"},
        )


if __name__ == "__main__":
    unittest.main()
