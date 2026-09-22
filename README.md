# HushMark API

Privacy-focused FastAPI service for detecting personally identifiable information in text and completed audio recordings. Automatic detection can miss or misclassify entities, so every result is returned as a reviewable span rather than being silently anonymized.

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
  "textLength": 37,
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

Email, Indian phone, PAN, Aadhaar, payment-card, and IP detections use deterministic patterns and checksum validation where applicable. Indian phone numbers may use digits or individually spoken English digit words. Person and address candidates use Presidio and require human review.

## Analyze audio

Create a local environment file from the committed template:

```bash
cp .env.example .env
# Add your key to .env, then start with:
uv run --env-file .env hushmark-api
```

`POST /v1/analyze/audio` accepts one completed audio recording as multipart form data. Files must be no larger than 25 MB and use `flac`, `m4a`, `mp3`, `mp4`, `mpeg`, `mpga`, `ogg`, `wav`, or `webm` format.

```bash
curl http://127.0.0.1:8000/v1/analyze/audio \
  --form file=@recording.webm
```

The service uses OpenAI's `gpt-transcribe` model with English and Hindi language hints, then analyzes the returned transcript. Detection offsets refer to the `transcript` field.

```json
{
  "transcript": "Email sample@example.com.",
  "analysisId": "ana_...",
  "textLength": 25,
  "detections": [
    {
      "id": "det_1",
      "type": "EMAIL",
      "start": 6,
      "end": 24,
      "confidence": 0.99,
      "source": "regex",
      "status": "pending"
    }
  ]
}
```

Raw audio is sent to OpenAI for transcription. HushMark does not intentionally persist the upload or transcript, and it does not log request bodies, filenames, transcripts, detected values, or placeholder mappings.

## Test

Tests use only synthetic PII.

```bash
uv run python -m unittest discover -s tests
```

The service does not persist submitted text and does not log request bodies or detected values.
