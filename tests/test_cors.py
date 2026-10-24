import json
import unittest

from fastapi.testclient import TestClient

from hushmark_api.main import app


class CorsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.origin = "http://localhost:3000"

    def test_json_preflight_allows_the_local_frontend(self) -> None:
        response = self.client.options(
            "/v1/redact",
            headers={
                "Origin": self.origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], self.origin)
        self.assertEqual(response.headers["access-control-allow-methods"], "POST")

    def test_preflight_rejects_an_unconfigured_origin(self) -> None:
        response = self.client.options(
            "/v1/redact",
            headers={
                "Origin": "https://unconfigured.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_export_and_validation_errors_have_cors_headers(self) -> None:
        for payload, status in (
            (
                {
                    "text": "Synthetic example.",
                    "review": {"analysisId": "ana_" + "0" * 32, "detections": []},
                },
                200,
            ),
            ({"text": ""}, 422),
        ):
            with self.subTest(status=status):
                response = self.client.post(
                    "/v1/redact", headers={"Origin": self.origin}, json=payload
                )

                self.assertEqual(response.status_code, status)
                self.assertEqual(
                    response.headers["access-control-allow-origin"], self.origin
                )
                self.assertIn(
                    "Content-Disposition", response.headers["access-control-expose-headers"]
                )

    def test_multipart_export_is_downloadable_from_the_frontend(self) -> None:
        response = self.client.post(
            "/v1/redact/document",
            headers={"Origin": self.origin},
            files={"file": ("example.txt", b"Synthetic example.", "text/plain")},
            data={
                "review": json.dumps(
                    {"analysisId": "ana_" + "0" * 32, "detections": []}
                )
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"Synthetic example.")
        self.assertIn("redacted.txt", response.headers["content-disposition"])
        self.assertEqual(response.headers["access-control-allow-origin"], self.origin)
        self.assertIn(
            "Content-Disposition", response.headers["access-control-expose-headers"]
        )
