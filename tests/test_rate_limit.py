import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from hushmark_api.rate_limit import RateLimitMiddleware


class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Mock(return_value=100.0)
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware, max_requests=2, clock=self.clock)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["https://frontend.example"],
            allow_methods=["POST"],
            allow_headers=["Content-Type"],
        )

        @app.post("/v1/example")
        def example() -> dict[str, str]:
            return {"status": "ok"}

        self.client = TestClient(app, client=("192.0.2.1", 12345))
        self.other_client = TestClient(app, client=("192.0.2.2", 12345))

    def test_limit_is_per_client_and_errors_preserve_cors(self) -> None:
        self.assertEqual(self.client.post("/v1/example").status_code, 200)
        self.assertEqual(self.client.post("/v1/example").status_code, 200)
        response = self.client.post(
            "/v1/example", headers={"Origin": "https://frontend.example"}
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "rate_limited")
        self.assertEqual(response.headers["retry-after"], "60")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(
            response.headers["access-control-allow-origin"], "https://frontend.example"
        )
        self.assertEqual(self.other_client.post("/v1/example").status_code, 200)

    def test_window_expires_and_preflight_does_not_consume_quota(self) -> None:
        for _ in range(3):
            response = self.client.options(
                "/v1/example",
                headers={
                    "Origin": "https://frontend.example",
                    "Access-Control-Request-Method": "POST",
                },
            )
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post("/v1/example").status_code, 200)
        self.assertEqual(self.client.post("/v1/example").status_code, 200)
        self.assertEqual(self.client.post("/v1/example").status_code, 429)
        self.clock.return_value = 160.0
        self.assertEqual(self.client.post("/v1/example").status_code, 200)

    def test_client_tracking_is_bounded(self) -> None:
        with patch("hushmark_api.rate_limit._MAX_CLIENTS", 1):
            self.client.post("/v1/example")
            self.client.post("/v1/example")
            self.other_client.post("/v1/example")
            self.assertEqual(self.client.post("/v1/example").status_code, 200)

    def test_enabled_limit_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            RateLimitMiddleware(FastAPI(), max_requests=-1)
