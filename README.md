# HushMark API

Privacy-focused FastAPI service for detecting personally identifiable information in plain text. Automatic detection can miss or misclassify entities, so every result is returned as a reviewable span rather than being silently anonymized.

## Run locally

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run hushmark-api
```

The API is available at `http://127.0.0.1:8000`, with interactive documentation at `/docs`.

## Analyze text

`POST /v1/analyze` accepts non-empty plain text up to 50,000 characters. The MVP currently supports the `en-IN` locale.

```json
{
  "text": "Contact Jane Doe at jane@example.com.",
  "locale": "en-IN"
}
```

The response contains zero-based, end-exclusive spans. It intentionally excludes detected values and placeholder mappings.

```json
{
  "analysisId": "ana_...",
  "textLength": 38,
  "detections": [
    {
      "id": "det_1",
      "type": "PERSON",
      "start": 8,
      "end": 16,
      "confidence": 0.85,
      "source": "presidio",
      "status": "pending"
    }
  ]
}
```

Email, Indian phone, PAN, Aadhaar, payment-card, and IP detections use deterministic patterns and checksum validation where applicable. Person and address candidates use Presidio and require human review.

## Test

Tests use only synthetic PII.

```bash
uv run python -m unittest discover -s tests
```

The service does not persist submitted text and does not log request bodies or detected values.
