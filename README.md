# HushMark API

Privacy-focused FastAPI service for detecting personally identifiable information in text and completed audio recordings. Automatic detection can miss or misclassify entities, so every result is returned as a reviewable span rather than being silently anonymized.

## Run locally

Requires Python 3.14, [uv](https://docs.astral.sh/uv/), and FFmpeg (`ffmpeg` and `ffprobe` on `PATH`) for audio export.

```bash
uv sync
cp .env.example .env
# Add your OpenAI API key to .env, then start with:
uv run --env-file .env hushmark-api
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
      "source": "openai",
      "status": "pending"
    }
  ]
}
```

Email, Indian phone, PAN, Aadhaar, payment-card, and IP detections use deterministic patterns and checksum validation where applicable. Indian phone numbers may use digits or individually spoken English digit words. Person-name candidates use OpenAI's `gpt-5.4-nano`; address candidates use Presidio. Every candidate requires human review.

## Analyze audio

`POST /v1/analyze/audio` accepts one completed audio recording as multipart form data. Files must be no larger than 25 MB and use `flac`, `m4a`, `mp3`, `mp4`, `mpeg`, `mpga`, `ogg`, `wav`, or `webm` format.

```bash
curl http://127.0.0.1:8000/v1/analyze/audio \
  --form file=@recording.webm
```

The service uses OpenAI's `whisper-1` model with word timestamps, then analyzes the returned transcript. Detection `start` and `end` refer to characters in `transcript`; `audioStartMs` and `audioEndMs` refer to zero-based, end-exclusive milliseconds in the original recording. Audio analysis fails if word timings cannot be aligned to the transcript.

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
      "status": "pending",
      "audioStartMs": 400,
      "audioEndMs": 1200
    }
  ]
}
```

## Beep approved audio detections

After the reviewer approves or rejects every detection, the frontend keeps the original recording and sends it again to `POST /v1/redact/audio`. The multipart `review` field is JSON containing the `analysisId` and the complete, ordered detection list. Each detection includes its returned `id`, `audioStartMs`, and `audioEndMs`, plus a final `status` of `approved` or `rejected`.

```bash
curl http://127.0.0.1:8000/v1/redact/audio \
  --form file=@recording.webm \
  --form 'review={"analysisId":"ana_00000000000000000000000000000000","detections":[{"id":"det_1","status":"approved","audioStartMs":400,"audioEndMs":1200}]}' \
  --output redacted.mp3
```

The response is an MP3. Approved intervals are widened by 100 ms on each side, merged if they touch, and have their original sound fully replaced by a beep. Rejected intervals are left audible. All rejected detections produce an MP3 transcode without beeps. The API rejects pending, malformed, duplicate, or out-of-duration review intervals. Because the service stores no analysis state, the frontend must submit the full original detection list; the API cannot verify that omitted detections or edited times match a past analysis. Human review and listening to the export remain necessary.

Validation errors retain the `{ "code": "invalid_review", "message": "..." }` shape. Messages name the field or decision to fix without returning submitted values.

Raw audio is sent to OpenAI for transcription, and submitted text or transcripts are sent to OpenAI for person-name detection. Responses API storage is disabled with `store=false`. HushMark does not intentionally persist uploads or text; audio export uses temporary files that are deleted after the response. The service does not log request bodies, filenames, transcripts, detected values, or placeholder mappings.

## Test

Tests use only synthetic PII.

```bash
uv run python -m unittest discover -s tests
```

The service does not persist submitted text and does not log request bodies or detected values.
