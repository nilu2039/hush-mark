# HushMark API Agent Guide

## Purpose

This repository contains the synchronous FastAPI service for HushMark's plain-text PII review workflow. Keep the service focused on detection and reviewable spans. Do not add persistence, queues, document uploads, OCR, audio, billing, teams, or external LLM processing unless explicitly requested.

## Commands

```bash
uv sync
uv run hushmark-api
uv run fastapi dev src/hushmark_api/main.py
uv run python -m unittest discover -s tests -v
```

After changing dependencies, update and commit `uv.lock`. The project targets Python 3.14.

## Current API Contract

- Endpoint: `POST /v1/analyze`
- Locale: `en-IN`
- Maximum text length: 50,000 characters
- Offsets: zero-based and end-exclusive
- The original input is immutable during analysis and review.
- Responses contain detection metadata, never detected values or placeholder mappings.
- Every automatic detection starts with `status="pending"`.
- Validation errors use the safe `{ "code": "...", "message": "..." }` shape and must not echo submitted text.

Do not silently change this contract. Update the versioned schemas, README, and tests together when the contract changes.

## Module Boundaries

- `main.py`: HTTP routes, safe error handling, and the CLI entry point.
- `schemas.py`: versioned public request and response models and enums.
- `recognizers.py`: deterministic recognizers, checksum validation, and the Presidio adapter.
- `overlap.py`: detector-independent overlap resolution.
- `service.py`: orchestration and stable detection ID assignment.

Keep request handlers thin. Detection rules do not belong in `main.py`.

## Detection Rules

Run detection in this order:

1. Deterministic patterns and validators for email, Indian phone, PAN, Aadhaar, payment cards, and IP addresses.
2. Presidio with `en_core_web_lg` for contextual person and address candidates.
3. Deterministic overlap resolution.

Structured validation takes priority over contextual NLP. Prefer the more specific type, then the longer span. Adjacent spans do not overlap. Do not use confidence to resolve conflicts between detector families because their scores are not calibrated against each other.

Treat confidence as a review-order hint, not a probability or guarantee. A valid format or checksum does not prove that a value is real PII.

## Privacy and Security

- Never log request bodies, original text, matched values, snippets, or mappings.
- Never retain submitted text by default.
- Never put real PII in tests, fixtures, examples, screenshots, or commit messages.
- Do not send text or snippets to an external service unless the user explicitly requests and approves that integration.
- Do not expose raw Presidio or spaCy objects through the API.
- Preserve whitespace and punctuation so offsets remain correct.
- Do not claim complete PII detection; human review remains required.

## Engineering Rules

- Use typed Python and small functions.
- Prefer the standard library and existing dependencies.
- Avoid speculative interfaces, factories, repositories, and infrastructure.
- Keep contextual detectors injectable as callables for fast unit tests.
- Add a runnable regression test for every recognizer, validator, overlap rule, or schema behavior change.
- Use synthetic values and assert exact end-exclusive offsets.
- Keep detection candidates free of original matched values.

When adding an entity type, update the enum, recognizer or adapter, overlap priority, README, and tests in the same change.

## Before Finishing

Run:

```bash
uv run python -m unittest discover -s tests -v
uv lock --check
uv pip check --python .venv/bin/python
git diff --check
```

Confirm that the API still returns only reviewable metadata and that no diagnostic output contains submitted text.
