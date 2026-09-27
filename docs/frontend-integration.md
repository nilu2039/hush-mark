# Frontend integration

This guide covers the review flow for pasted text, UTF-8 text files, and completed audio recordings. The API detects possible PII, the frontend lets a person review every mark, and an export endpoint applies the final decisions. The API does not store review state.

Base URL for local development: `http://127.0.0.1:8000`. Interactive API documentation is at `/docs`.

## Endpoints

| Input | Analyze | Export | Successful export |
| --- | --- | --- | --- |
| Pasted text | `POST /v1/analyze` with JSON | `POST /v1/redact` with JSON | UTF-8 `text/plain` body |
| `.txt`, `.md`, `.markdown` | `POST /v1/analyze/document` with multipart `file` | `POST /v1/redact/document` with multipart `file` and JSON-string `review` | File download, `text/plain` or `text/markdown` |
| Audio | `POST /v1/analyze/audio` with multipart `file` | `POST /v1/redact/audio` with multipart `file` and JSON-string `review` | MP3 download |

Pasted text and decoded text files must contain non-whitespace content and have at most 50,000 characters. Text files must be UTF-8 and at most 256 KiB. Audio must be at most 25 MiB and have a `flac`, `m4a`, `mp3`, `mp4`, `mpeg`, `mpga`, `ogg`, `wav`, or `webm` extension. Text analysis accepts `locale: "en-IN"`; that is the only supported locale.

## Review state and IDs

Keep the original input, the returned `analysisId`, and a working copy of detections in memory. Do not alter the original input while reviewing. If the user changes the input, start a new analysis and reset the marks.

- Analysis returns automatic marks with IDs `det_1`, `det_2`, and so on. Keep each ID when changing its type, character range, audio range, or decision. Every automatic mark should be submitted at export, including rejected ones.
- When the reviewer adds a missed mark, assign a unique `man_N` ID in the frontend, such as `man_1`. The API does not issue manual IDs. Manual IDs need not be consecutive, but they must be unique within the review.
- Automatic IDs submitted in a review must form `det_1` through `det_N` with no gaps. Marks can appear in any array order.
- Automatic marks begin with `status: "pending"`. Export accepts only `"approved"` or `"rejected"`; require a decision for each mark before sending it.
- The `source` and `confidence` fields from analysis help display the result. Do not include them in export review entries. A manual mark is identified by its `man_N` ID; export does not return a new detection list.

The server checks the **format** of `analysisId` (`ana_` plus 32 lowercase hexadecimal characters). It does **not** check whether that ID, the input, or the marks match an earlier analysis. A changed but well-formed ID can still be accepted. Treat `analysisId` as a correlation label, not authorization or proof of provenance. Keep the correct input and review together in the frontend.

Allowed PII types are `PERSON`, `EMAIL`, `PHONE`, `ADDRESS`, `DATE_OF_BIRTH`, `IP_ADDRESS`, `AADHAAR`, `PAN`, `BANK_ACCOUNT`, and `PAYMENT_CARD`.

The export payloads use these shapes. Keep `"pending"` in the editable UI state, then send only final decisions:

```ts
type PiiType =
  | "PERSON" | "EMAIL" | "PHONE" | "ADDRESS" | "DATE_OF_BIRTH"
  | "IP_ADDRESS" | "AADHAAR" | "PAN" | "BANK_ACCOUNT" | "PAYMENT_CARD";
type Decision = "approved" | "rejected";
type TextMark = {
  id: string; type: PiiType; start: number; end: number; status: Decision;
};
type AudioMark = {
  id: string; type?: PiiType; audioStartMs: number; audioEndMs: number;
  status: Decision;
};
type Review<Mark> = { analysisId: string; detections: Mark[] };
```

`AudioMark.type` is required at runtime when `id` starts with `man_`. Generate the next manual ID locally and check that it is not already in the working mark list. Keep a rejected automatic mark in the list; deleting it may create a gap in `det_N` IDs.

```ts
function nextManualId(marks: { id: string }[]): string {
  const used = new Set(marks.map((mark) => mark.id));
  for (let n = 1; ; n++) {
    const id = `man_${n}`;
    if (!used.has(id)) return id;
  }
}
```

## Pasted text flow

1. Save the exact original string. Analyze it with `POST /v1/analyze` using JSON `{ "text": originalText, "locale": "en-IN" }`.
2. Copy the response's `analysisId` and `detections` into review state. The response contains `textLength` and detection metadata; it does not return the text.
3. Let the reviewer approve, reject, change type or bounds, or add a `man_N` mark. Keep all coordinates relative to the original string.
4. Send the original string and final review to `POST /v1/redact`. Read the successful response with `response.text()`, not `response.json()`.

An export request with one manually added mark looks like this:

```json
{
  "text": "Email sample@example.com.",
  "review": {
    "analysisId": "ana_00000000000000000000000000000000",
    "detections": [
      {
        "id": "man_1",
        "type": "EMAIL",
        "start": 6,
        "end": 24,
        "status": "approved"
      }
    ]
  }
}
```

The response body is `Email [EMAIL].`. Use the actual `analysisId` from analysis in the application; the zero-filled ID above is only an example.

```ts
const exportResponse = await fetch("/v1/redact", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    text: originalText,
    review: { analysisId: analysis.analysisId, detections: finalTextMarks },
  }),
});
if (!exportResponse.ok) throw new Error((await exportResponse.json()).message);
const redactedText = await exportResponse.text();
```

Text spans are zero-based and end-exclusive: `[start, end)`. Both must be integers, `0 <= start < end <= originalText length` in Unicode code points. Approved spans must not overlap; rejected marks may overlap other marks. Approved spans become `[TYPE]` placeholders. Rejected spans and surrounding punctuation and whitespace remain unchanged.

**Browser offset conversion:** JavaScript string indexes and `<textarea>` selection offsets use UTF-16 code units. The API uses Python character offsets (Unicode code points). Convert selection offsets before sending them when the original text may contain emoji or other characters outside the Basic Multilingual Plane:

```ts
function apiOffset(original: string, utf16Offset: number): number {
  return Array.from(original.slice(0, utf16Offset)).length;
}

const start = apiOffset(originalText, textarea.selectionStart);
const end = apiOffset(originalText, textarea.selectionEnd);
```

For a rendered view or multiple DOM text nodes, first map the selection back to offsets in the **exact original string**. Do not calculate offsets from HTML, a normalized copy, or a redacted preview.

## Text file flow

Analyze the selected `File` with a multipart `file` field. The response adds `text` to the usual analysis fields. Show that returned text for review and use its character offsets; a leading UTF-8 BOM has been removed from this returned text. Keep the same original `File` object for export. Export with a multipart `file` field and a `review` field containing `JSON.stringify(review)`.

The returned `text` preserves CRLF line endings. A `<textarea>` may normalize them to LF, so its selection offsets cannot be used directly against the returned text for CRLF files. Keep the returned string unchanged and map selections back to its offsets, or use a read-only viewer that preserves the raw text positions. Apply the Unicode offset conversion above as well.

```ts
const analyzeForm = new FormData();
analyzeForm.append("file", originalFile);
const analysisResponse = await fetch("/v1/analyze/document", {
  method: "POST",
  body: analyzeForm,
});
if (!analysisResponse.ok) throw new Error((await analysisResponse.json()).message);
const analysis = await analysisResponse.json();

const exportForm = new FormData();
exportForm.append("file", originalFile);
exportForm.append("review", JSON.stringify({
  analysisId: analysis.analysisId,
  detections: finalTextMarks,
}));
const exportResponse = await fetch("/v1/redact/document", {
  method: "POST",
  body: exportForm,
});
if (!exportResponse.ok) throw new Error((await exportResponse.json()).message);
const redactedFile = await exportResponse.blob();
```

Do not set the `Content-Type` header yourself for `FormData`; the browser supplies the multipart boundary. The download keeps the input extension and leading UTF-8 BOM, if any. Markdown syntax is preserved as text, although a placeholder inserted within Markdown syntax may change how it renders.

## Audio flow

Analyze the original recording with multipart `file`. The response includes a `transcript`, `textLength`, and detections with both transcript character offsets (`start`, `end`) and recording times (`audioStartMs`, `audioEndMs`). Show the transcript for context and use the recording times to position marks in an audio player or waveform.

For a new manual mark, let the reviewer select a time interval while listening. Convert to integer milliseconds (for example, `Math.round(seconds * 1000)`) and send `audioStartMs < audioEndMs`. Its `type` is required. A type is optional for an automatic audio mark, so older clients that omit it still work. Do not send transcript `start` or `end` in the audio export review.

```json
{
  "analysisId": "ana_00000000000000000000000000000000",
  "detections": [
    {
      "id": "det_1",
      "type": "EMAIL",
      "status": "rejected",
      "audioStartMs": 400,
      "audioEndMs": 1200
    },
    {
      "id": "man_1",
      "type": "PHONE",
      "status": "approved",
      "audioStartMs": 1500,
      "audioEndMs": 2200
    }
  ]
}
```

Send that object as `JSON.stringify(review)` in a multipart `review` field, alongside the **same original recording** in `file`, to `POST /v1/redact/audio`. Read the successful response as a blob and offer it as an MP3 download. Every interval, including a rejected one, must fit within the recording's duration. The server adds 100 ms on each side of approved intervals, clips at the recording ends, merges touching intervals, and replaces the sound there with a beep. Rejected intervals remain audible.

```ts
const exportForm = new FormData();
exportForm.append("file", originalRecording);
exportForm.append("review", JSON.stringify({
  analysisId: analysis.analysisId,
  detections: finalAudioMarks,
}));
const exportResponse = await fetch("/v1/redact/audio", {
  method: "POST",
  body: exportForm,
});
if (!exportResponse.ok) throw new Error((await exportResponse.json()).message);
const redactedMp3 = await exportResponse.blob();
```

## Errors and privacy

Check `response.ok` before reading an export as text or a blob. Error responses are JSON with `{ "code": "...", "message": "..." }`. Typical cases include `422 invalid_review` for pending, malformed, overlapping approved text, or out-of-range marks; `413` for oversized uploads; `415` for unsupported file types; and `503` when analysis or audio processing is unavailable. Display the safe `message`; do not log or report the submitted text, transcript, file contents, or selected snippets.

The API does not store review state. Keep review data in memory unless your product deliberately adds its own secure persistence. The export responses use `Cache-Control: no-store`. Revoke browser object URLs after downloads. Audio analysis sends the recording to OpenAI for transcription; text, decoded files, and transcripts are sent to OpenAI for person-name detection.

The FastAPI app currently has no CORS middleware. A browser frontend on another origin must use a same-origin API proxy or have the backend configured to allow that specific origin.
