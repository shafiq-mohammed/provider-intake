"""The real Anthropic adapters for OCR and field extraction (T-007).

Both adapters take a client object rather than building one, so tests drive them with a stub
that records ``messages.create(**kwargs)``. ``make_client()`` is the only place that builds a
real ``anthropic.Anthropic``, and it refuses to build one without an API key (fail fast at
startup, SPEC T-007).

Every failure leaving this module is an ``OcrError`` or an ``ExtractorError`` whose message
names neither the upstream error text nor any document bytes or extracted text (SPEC A17); the
original exception travels only as ``__cause__``.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import anthropic

from app.extraction import ExtractorError
from app.models import FIELD_NAMES, ExtractionResult, FieldResult, FieldStatus
from app.ocr import OcrError
from app.settings import Settings

OCR_PROMPT = (
    "Transcribe all text in this document verbatim. Preserve the reading order and the line "
    "breaks, and do not correct, summarize, translate or comment on anything. Output only the "
    "transcribed text; if the document carries no legible text, output nothing."
)

EXTRACTION_PROMPT = (
    "You are reading the transcribed text of a US healthcare provider credential document. "
    "Extract exactly these four fields: "
    "provider_name (the provider's full name), "
    "license_number (the licence or certificate number), "
    "state (the issuing US state), "
    "expiration_date (the licence expiration date). "
    "Your job is to transcribe what the document says, not to judge it. "
    "Do not decide whether the document is authentic or genuine, whether a value is plausible, "
    "or whether it matches the format you would expect -- a value you can read is a value you "
    "report, as written. "
    "Reply with a single JSON object and nothing else: no prose, no explanation. "
    "Its keys are the four field names above, and each value is an object with the keys "
    "{value, status, raw, issues}. "
    '"value" is the value as it appears, with surrounding labels and whitespace trimmed, or '
    "null when there is none. "
    '"status" is one of "found" (the field is in the text and you could read it), '
    '"not_found" (the field is absent from the text), or '
    '"unreadable" (the field is present but you cannot read it, for example because it is '
    "illegible, obscured or cut off). "
    '"raw" is the exact substring you read the value from, or null. '
    '"issues" is a list of short snake_case strings, or an empty list -- use it to record any '
    "concern you have about the field, for example appears_fictional, not_a_standard_format, "
    "low_contrast or handwritten. "
    'Whenever the text is legible, keep the status "found" and put the doubt in "issues": a '
    "value that looks suspicious, fictional or oddly formatted is still found, with your "
    "concern named beside it rather than in place of it. "
    'Never return the status "invalid" -- it is reserved for the deterministic validation that '
    "runs after you and alone decides whether a value is usable. "
    "Never guess: a field you cannot read is unreadable and a field that is not there is "
    "not_found, neither is found. "
    "The document text follows."
)

_IMAGE_MEDIA_TYPES: frozenset[str] = frozenset({"image/jpeg", "image/png"})
_PDF_MEDIA_TYPE = "application/pdf"

_FENCE = "```"


def response_text(message: Any) -> str:
    """Concatenate ``block.text`` for every text block of ``message``, ignoring other blocks.

    A message carrying no text block (tool use, thinking only, or nothing at all) is the empty
    string, not an error.
    """
    parts: list[str] = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(str(getattr(block, "text", "")))
    return "".join(parts)


def _document_block(content: bytes, content_type: str) -> dict[str, Any]:
    """Build the image or document content block for ``content``.

    Raises ``OcrError`` for any other content type, before the client is touched.
    """
    if content_type in _IMAGE_MEDIA_TYPES:
        block_type = "image"
    elif content_type == _PDF_MEDIA_TYPE:
        block_type = "document"
    else:
        raise OcrError(f"vision OCR does not support content type {content_type!r}")
    return {
        "type": block_type,
        "source": {
            "type": "base64",
            "media_type": content_type,
            "data": base64.b64encode(content).decode(),
        },
    }


class AnthropicOcrEngine:
    """An ``OcrEngine`` backed by one Claude vision call per document."""

    def __init__(self, client: Any, model: str, *, max_tokens: int = 4096) -> None:
        """``client`` is an ``anthropic.Anthropic`` or anything exposing ``messages.create``."""
        self.client = client
        self.model = model
        self.max_tokens = max_tokens

    def extract_text(self, content: bytes, content_type: str) -> str:
        """Return the transcription of ``content``; any failure is an ``OcrError``."""
        block = _document_block(content, content_type)
        messages = [
            {
                "role": "user",
                "content": [block, {"type": "text", "text": OCR_PROMPT}],
            }
        ]
        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=messages,
            )
        except Exception as exc:
            raise OcrError("the vision OCR call failed") from exc
        return response_text(message)


class AnthropicFieldExtractor:
    """A ``FieldExtractor`` backed by one Claude call per document text."""

    def __init__(self, client: Any, model: str, *, max_tokens: int = 1024) -> None:
        """``client`` is an ``anthropic.Anthropic`` or anything exposing ``messages.create``."""
        self.client = client
        self.model = model
        self.max_tokens = max_tokens

    def extract_fields(self, text: str) -> ExtractionResult:
        """Return the four fields read out of ``text``; any failure is an ``ExtractorError``."""
        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": f"{EXTRACTION_PROMPT}\n\n{text}"}],
            }
        ]
        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=messages,
            )
        except Exception as exc:
            raise ExtractorError("the field extraction call failed") from exc
        return parse_extraction_json(response_text(message))


def _strip_fences(text: str) -> str:
    """Strip surrounding whitespace and one optional ``` / ```json fence."""
    stripped = text.strip()
    if not stripped.startswith(_FENCE):
        return stripped
    newline = stripped.find("\n")
    body = "" if newline == -1 else stripped[newline + 1 :]
    body = body.rstrip()
    if body.endswith(_FENCE):
        body = body[: -len(_FENCE)]
    return body.strip()


def _coerce_text(value: Any) -> str | None:
    """Return ``None`` for ``None``, the string itself for a string, else ``str(value)``."""
    if value is None or isinstance(value, str):
        return value
    return str(value)


def _coerce_issues(value: Any) -> list[str]:
    """Return the issues as a list of strings; anything that is not a list becomes ``[]``."""
    if not isinstance(value, list):
        return []
    return [item if isinstance(item, str) else str(item) for item in value]


def _field_from_payload(payload: Any) -> FieldResult:
    """Build one ``FieldResult`` from one untrusted field object of the model's reply."""
    if not isinstance(payload, dict):
        return FieldResult(status=FieldStatus.NOT_FOUND)
    issues = _coerce_issues(payload.get("issues"))
    try:
        status = FieldStatus(payload.get("status"))
    except (ValueError, TypeError):
        status = FieldStatus.UNREADABLE
        issues = [*issues, "unknown_status"]
    return FieldResult(
        value=_coerce_text(payload.get("value")),
        status=status,
        raw=_coerce_text(payload.get("raw")),
        issues=issues,
    )


def parse_extraction_json(text: str) -> ExtractionResult:
    """Parse a model reply into an ``ExtractionResult``.

    Only fence stripping is done: a reply with prose around the JSON is rejected rather than
    salvaged by searching for a brace. Anything that is not a JSON object, and any field the
    reply omits or sends as a non-object, is an ``ExtractorError`` or a ``not_found`` field
    respectively. No part of ``text`` ever reaches the error message (SPEC A17).
    """
    try:
        payload = json.loads(_strip_fences(text))
    except ValueError as exc:
        raise ExtractorError("the model reply was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ExtractorError("the model reply was not a JSON object")
    return ExtractionResult(
        **{name: _field_from_payload(payload.get(name)) for name in FIELD_NAMES}
    )


def make_client(settings: Settings) -> anthropic.Anthropic:
    """Build the real client; a missing or empty ``anthropic_api_key`` is a ``ValueError``."""
    api_key = settings.anthropic_api_key
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is required for the 'anthropic' backends")
    return anthropic.Anthropic(api_key=api_key)
