"""Tests for T-007: the real Anthropic adapters selected by settings.

Two hard boundaries are enforced by this file itself:

1. Only the AC6 tests may touch the network. Every other test drives the adapters with a stub
   client -- an object exposing ``messages.create(**kwargs)`` that records the kwargs and replays
   a canned message. The stub is an *input*: it records and replays, it never base64-encodes,
   parses JSON, chooses a block type or decides an error code. That is the behavior under test.
   Several tests additionally install the ``no_network`` guard, which turns any socket use into a
   failure rather than a silent outbound call.
2. ``os.environ`` is read in exactly three places: the two module-level skip guards below and the
   AC6 live tests (through ``Settings.from_env()``). Every other ``Settings`` is constructed
   explicitly, so the suite is hermetic (SPEC A13). The key's value is never printed, logged or
   asserted on; only its presence is ever observed.

The module under test is imported lazily by ``adapters()`` rather than at module scope. That is
deliberate: while ``src/app/anthropic_adapters.py`` does not exist, a top-level import would be a
collection error for the whole file and the AC6 live tests would be reported as errors instead of
skips -- a skip that is really a missing import is indistinguishable from a pass.
"""

import base64
import json
import os
import socket
import types
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import extraction, models, ocr, validation
from app.main import create_app
from app.settings import Settings

# --------------------------------------------------------------------------- constants

MODEL = "model-x"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PDF_BYTES = b"%PDF-1.4\n" + b"\x00" * 32

OCR_REPLY = "hello from vision"
OCR_TEXT = "License text"

# Marker strings that must never leak out of an adapter failure (SPEC A17).
SECRET = "SECRET-DOCUMENT-CONTENT"

# The AC3 response, verbatim from the ticket: a fence, one good field and one invented status.
FENCED_RESPONSE = """```json
{"provider_name": {"value": "Jane Doe", "status": "found", "raw": "Jane Doe", "issues": []},
 "state": {"value": "CA", "status": "maybe"}}
```"""

# The AC3 edge case: no fence, a non-list ``issues``, numeric ``value``/``raw``, a non-object
# field and one field missing altogether.
BARE_RESPONSE = """
{"provider_name": {"value": 12345, "status": "found", "raw": 678, "issues": "oops"},
 "license_number": {"value": "LCSW-1", "status": "found", "raw": null, "issues": ["faint"]},
 "state": "CA"}
"""

# A fence with no language tag and slack whitespace around it.
UNTAGGED_FENCE_RESPONSE = (
    "\n  ```\n"
    '{"provider_name": {"value": "Jane Doe", "status": "found"},'
    ' "license_number": {"value": "LCSW-1", "status": "found"},'
    ' "state": {"value": "CA", "status": "found"},'
    ' "expiration_date": {"value": "2099-12-31", "status": "found"}}'
    "\n  ```  \n"
)

FOUR_FIELDS = {
    "provider_name": {"value": "Jane Doe", "status": "found", "raw": "Jane Q. Doe", "issues": []},
    "license_number": {"value": "LCSW-12345", "status": "found", "raw": "LCSW-12345", "issues": []},
    "state": {"value": "CA", "status": "found", "raw": "California", "issues": []},
    "expiration_date": {
        "value": "2099-12-31",
        "status": "found",
        "raw": "12/31/2099",
        "issues": ["low_contrast"],
    },
}
FOUR_FIELDS_JSON = json.dumps(FOUR_FIELDS)

PROSE_THEN_FENCE_RESPONSE = f"Here is the JSON you asked for:\n```json\n{FOUR_FIELDS_JSON}\n```"

# The text handed to the live extractor (AC6). It contains no real provider's details.
LIVE_TEXT = (
    "Licensed Clinical Social Worker. Name: Jane Q. Doe. License No. LCSW-12345. "
    "State of California. Expires: 12/31/2099"
)

# --------------------------------------------------------------------------- live-test guards
# The only reads of os.environ in this file. Presence only: the value is never touched.

SAMPLE_PNG = Path(__file__).parent / "fixtures" / "sample_license.png"
HAS_API_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))
HAS_SAMPLE_PNG = SAMPLE_PNG.is_file()

requires_api_key = pytest.mark.skipif(
    not HAS_API_KEY,
    reason="live test skipped: ANTHROPIC_API_KEY is not set in the environment",
)
requires_sample_png = pytest.mark.skipif(
    not HAS_SAMPLE_PNG,
    reason=f"live test skipped: the sample document {SAMPLE_PNG} does not exist (SPEC A15)",
)

# --------------------------------------------------------------------------- input factories


class StubClient:
    """Input factory only: records every ``messages.create`` kwargs and replays a canned reply.

    It does not encode, parse or validate anything. ``texts`` become text blocks; ``blocks``
    replaces the block list wholesale (for the zero-text-block case); ``error`` makes the call
    raise instead of returning, which is how a client failure is simulated.
    """

    def __init__(
        self,
        *texts: str,
        blocks: list[Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._error = error
        if blocks is not None:
            self._blocks: list[Any] = list(blocks)
        else:
            self._blocks = [types.SimpleNamespace(type="text", text=text) for text in texts]
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return types.SimpleNamespace(content=list(self._blocks))


def adapters() -> Any:
    """Import the module under test at call time (see the module docstring)."""
    from app import anthropic_adapters

    return anthropic_adapters


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard, not a stub: opening a socket during the test is a failure, not a silent call."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("this test must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def sole_call(stub: StubClient) -> dict[str, Any]:
    """Read helper: assert exactly one call was made and return its kwargs."""
    assert len(stub.calls) == 1, f"expected exactly one messages.create call, got {len(stub.calls)}"
    kwargs = stub.calls[0]
    assert set(kwargs) == {"model", "max_tokens", "messages"}, sorted(kwargs)
    return kwargs


def user_content(kwargs: dict[str, Any]) -> Any:
    """Read helper: the content of the single user message in a recorded call."""
    messages = kwargs["messages"]
    assert isinstance(messages, list)
    assert len(messages) == 1, messages
    assert messages[0]["role"] == "user"
    return messages[0]["content"]


def flatten_text(content: Any) -> str:
    """Read helper: the text a request carries, whether content is a string or a block list."""
    if isinstance(content, str):
        return content
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def b64(data: bytes) -> str:
    """Read helper: the wire encoding of document bytes, from the standard library only."""
    return base64.b64encode(data).decode()


def dump(field: Any) -> dict[str, Any]:
    """Read helper: the JSON shape of a FieldResult, for whole-object comparison."""
    return field.model_dump(mode="json")


def upload_png(client: TestClient) -> str:
    """Factory only: upload one PNG through the real endpoint and return its id."""
    response = client.post("/documents", files={"file": ("license.png", PNG_BYTES, "image/png")})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


# --------------------------------------------------------------------------- AC1


def test_ac1_png_request_shape_and_returned_text() -> None:
    """Happy path: one call, the documented image block, OCR_PROMPT last, the text returned."""
    mod = adapters()
    stub = StubClient(OCR_REPLY)
    engine = mod.AnthropicOcrEngine(stub, MODEL)

    text = engine.extract_text(PNG_BYTES, "image/png")

    assert text == OCR_REPLY
    kwargs = sole_call(stub)
    assert kwargs["model"] == MODEL
    assert kwargs["max_tokens"] == 4096
    content = user_content(kwargs)
    assert len(content) >= 2
    assert content[0] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": b64(PNG_BYTES),
        },
    }
    assert content[-1] == {"type": "text", "text": mod.OCR_PROMPT}
    assert isinstance(mod.OCR_PROMPT, str) and mod.OCR_PROMPT.strip() != ""


def test_ac1_jpeg_media_type_is_the_declared_content_type() -> None:
    """Edge: the media_type is whatever type was declared, and the data is that image's bytes."""
    mod = adapters()
    stub = StubClient(OCR_REPLY)
    engine = mod.AnthropicOcrEngine(stub, MODEL, max_tokens=11)

    assert engine.extract_text(JPEG_BYTES, "image/jpeg") == OCR_REPLY

    kwargs = sole_call(stub)
    assert kwargs["max_tokens"] == 11
    content = user_content(kwargs)
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert content[0]["source"]["data"] == b64(JPEG_BYTES)
    assert content[0]["source"]["data"] != b64(PNG_BYTES)
    assert content[0]["type"] == "image"
    assert content[-1] == {"type": "text", "text": mod.OCR_PROMPT}


def test_ac1_message_with_no_text_blocks_yields_empty_text() -> None:
    """Failure path: a reply carrying no text block is an empty string, not an exception."""
    mod = adapters()
    no_text = StubClient(blocks=[types.SimpleNamespace(type="tool_use", id="t1")])
    engine = mod.AnthropicOcrEngine(no_text, MODEL)

    assert engine.extract_text(PNG_BYTES, "image/png") == ""
    assert len(no_text.calls) == 1

    empty = StubClient(blocks=[])
    assert mod.AnthropicOcrEngine(empty, MODEL).extract_text(PNG_BYTES, "image/png") == ""
    assert mod.response_text(types.SimpleNamespace(content=[])) == ""


# --------------------------------------------------------------------------- AC2


def test_ac2_pdf_uses_a_document_block() -> None:
    """Happy path: a PDF that reached the engine is sent as a base64 document block."""
    mod = adapters()
    stub = StubClient(OCR_REPLY)

    text = mod.AnthropicOcrEngine(stub, MODEL).extract_text(PDF_BYTES, "application/pdf")

    assert text == OCR_REPLY
    content = user_content(sole_call(stub))
    assert content[0] == {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": b64(PDF_BYTES),
        },
    }
    assert content[-1] == {"type": "text", "text": mod.OCR_PROMPT}


def test_ac2_response_text_concatenates_every_text_block() -> None:
    """Edge: two text blocks are joined, and non-text blocks are ignored."""
    mod = adapters()
    stub = StubClient("first half ", "second half")

    assert (
        mod.AnthropicOcrEngine(stub, MODEL).extract_text(PNG_BYTES, "image/png")
        == "first half second half"
    )

    message = types.SimpleNamespace(
        content=[
            types.SimpleNamespace(type="text", text="alpha"),
            types.SimpleNamespace(type="thinking", thinking="ignored"),
            types.SimpleNamespace(type="text", text="beta"),
        ]
    )
    assert mod.response_text(message) == "alphabeta"


def test_ac2_unsupported_content_type_raises_before_any_call() -> None:
    """Failure path: an unsupported type never reaches the API, and a client error is OcrError."""
    mod = adapters()
    stub = StubClient(OCR_REPLY)
    engine = mod.AnthropicOcrEngine(stub, MODEL)

    with pytest.raises(ocr.OcrError):
        engine.extract_text(b"x", "text/plain")

    assert stub.calls == []

    boom = RuntimeError(f"stub failure: {SECRET}")
    failing = StubClient(error=boom)

    with pytest.raises(ocr.OcrError) as exc_info:
        mod.AnthropicOcrEngine(failing, MODEL).extract_text(PNG_BYTES, "image/png")

    assert exc_info.value.__cause__ is boom
    # A17: the wrapper names no content and repeats no upstream message.
    assert SECRET not in str(exc_info.value)
    assert b64(PNG_BYTES) not in str(exc_info.value)


def test_ac2_endpoint_reports_only_the_exception_class_name(no_network: None) -> None:
    """A17 end to end: an engine failure surfaces a class name, never bytes or a message."""
    mod = adapters()
    failing = StubClient(error=RuntimeError(f"stub failure: {SECRET}"))
    client = TestClient(
        create_app(ocr_engine=mod.AnthropicOcrEngine(failing, MODEL)),
    )
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/text")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ocr_failed"
    assert response.json()["error"]["details"] == {"reason": "OcrError"}
    body = response.text
    assert SECRET not in body
    assert b64(PNG_BYTES) not in body
    assert "RuntimeError" not in body


# --------------------------------------------------------------------------- AC3


def test_ac3_fenced_json_with_an_unknown_status_is_downgraded() -> None:
    """Happy path: the fence is stripped, and an invented status becomes unreadable."""
    mod = adapters()

    result = mod.parse_extraction_json(FENCED_RESPONSE)

    assert isinstance(result, models.ExtractionResult)
    assert dump(result.provider_name) == {
        "value": "Jane Doe",
        "status": "found",
        "raw": "Jane Doe",
        "issues": [],
    }
    # The model claimed "CA" under a status that does not exist; A8 nulls the value.
    assert result.state.status == models.FieldStatus.UNREADABLE
    assert result.state.value is None
    assert result.state.issues == ["unknown_status"]
    for name in ("license_number", "expiration_date"):
        field = getattr(result, name)
        assert field.status == models.FieldStatus.NOT_FOUND, name
        assert field.value is None, name
        assert field.raw is None, name
        assert field.issues == [], name
    assert result.missing_fields == ["license_number", "state", "expiration_date"]


def test_ac3_bare_json_coerces_value_raw_and_issues() -> None:
    """Edge: no fence, non-string value/raw coerced, non-list issues dropped, non-object field."""
    mod = adapters()

    result = mod.parse_extraction_json(BARE_RESPONSE)

    assert dump(result.provider_name) == {
        "value": "12345",
        "status": "found",
        "raw": "678",
        "issues": [],
    }
    assert dump(result.license_number) == {
        "value": "LCSW-1",
        "status": "found",
        "raw": None,
        "issues": ["faint"],
    }
    # "state": "CA" is a string, not a field object, so nothing was read for it.
    assert dump(result.state) == {"value": None, "status": "not_found", "raw": None, "issues": []}
    # expiration_date was absent from the response entirely.
    assert result.expiration_date.status == models.FieldStatus.NOT_FOUND
    assert result.missing_fields == ["state", "expiration_date"]

    # An untagged fence and slack whitespace parse the same way.
    untagged = mod.parse_extraction_json(UNTAGGED_FENCE_RESPONSE)
    assert untagged.missing_fields == []
    assert untagged.state.value == "CA"
    assert untagged.expiration_date.value == "2099-12-31"


def test_ac3_malformed_and_non_object_json_raise_extractor_error() -> None:
    """Failure path: untrusted text that is not a JSON object is an ExtractorError."""
    mod = adapters()

    for text in ("not json", "[1, 2]", "", "   ", "null", '"a string"', "```json\n{oops}\n```"):
        with pytest.raises(extraction.ExtractorError):
            mod.parse_extraction_json(text)


# --------------------------------------------------------------------------- AC4


def test_ac4_extract_fields_round_trips_a_four_field_response() -> None:
    """Happy path: one call carrying the prompt and the text; the JSON becomes the result."""
    mod = adapters()
    stub = StubClient(FOUR_FIELDS_JSON)

    result = mod.AnthropicFieldExtractor(stub, MODEL).extract_fields(OCR_TEXT)

    assert isinstance(result, models.ExtractionResult)
    for name, expected in FOUR_FIELDS.items():
        assert dump(getattr(result, name)) == expected, name
    assert result.missing_fields == []

    kwargs = sole_call(stub)
    assert kwargs["model"] == MODEL
    assert kwargs["max_tokens"] == 1024
    request_text = flatten_text(user_content(kwargs))
    assert mod.EXTRACTION_PROMPT in request_text
    assert OCR_TEXT in request_text
    assert "json" in mod.EXTRACTION_PROMPT.lower()
    for name in models.FIELD_NAMES:
        assert name in mod.EXTRACTION_PROMPT, name


def test_ac4_prose_before_the_fence_is_not_parsed() -> None:
    """Edge, and a documented limitation: only fence-stripping is done, never substring search.

    Leading prose means the text after fence-stripping is not JSON, so the response is rejected
    rather than salvaged. Pinned here so the limitation is a decision, not a surprise.
    """
    mod = adapters()
    stub = StubClient(PROSE_THEN_FENCE_RESPONSE)

    with pytest.raises(extraction.ExtractorError):
        mod.AnthropicFieldExtractor(stub, MODEL).extract_fields(OCR_TEXT)

    assert len(stub.calls) == 1

    with pytest.raises(extraction.ExtractorError):
        mod.parse_extraction_json(PROSE_THEN_FENCE_RESPONSE)

    # Control: the very same JSON without the prose parses.
    assert mod.parse_extraction_json(f"```json\n{FOUR_FIELDS_JSON}\n```").missing_fields == []


def test_ac4_client_exception_becomes_extractor_error() -> None:
    """Failure path: the client's exception is the cause and its message does not travel."""
    mod = adapters()
    boom = RuntimeError(f"stub failure: {SECRET}")
    stub = StubClient(error=boom)

    with pytest.raises(extraction.ExtractorError) as exc_info:
        mod.AnthropicFieldExtractor(stub, MODEL).extract_fields(OCR_TEXT)

    assert exc_info.value.__cause__ is boom
    assert SECRET not in str(exc_info.value)
    assert len(stub.calls) == 1


def test_ac4_endpoint_reports_only_the_extractor_exception_class_name(no_network: None) -> None:
    """A17 end to end: an extractor failure surfaces a class name, never the OCR text."""
    mod = adapters()
    stub = StubClient(error=RuntimeError(f"stub failure: {SECRET}"))
    client = TestClient(
        create_app(
            ocr_engine=ocr.FakeOcrEngine(SECRET),
            field_extractor=mod.AnthropicFieldExtractor(stub, MODEL),
        )
    )
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "extraction_failed"
    assert response.json()["error"]["details"] == {"reason": "ExtractorError"}
    assert SECRET not in response.text
    assert "RuntimeError" not in response.text


# --------------------------------------------------------------------------- AC5


def test_ac5_anthropic_backend_without_a_key_fails_fast() -> None:
    """Happy path: no key is a startup ValueError, raised by the client factory itself."""
    settings = Settings(
        ocr_backend="anthropic", extractor_backend="anthropic", anthropic_api_key=None
    )

    with pytest.raises(ValueError):
        create_app(settings)

    # The same failure, located: it is the missing key, not an unknown backend name.
    with pytest.raises(ValueError):
        adapters().make_client(settings)

    # Either backend alone is enough to refuse to start.
    with pytest.raises(ValueError):
        create_app(Settings(ocr_backend="anthropic"))
    with pytest.raises(ValueError):
        create_app(Settings(extractor_backend="anthropic"))


def test_ac5_empty_api_key_is_rejected(no_network: None) -> None:
    """Edge: an empty string is as absent as None, at the factory and at startup."""
    mod = adapters()

    with pytest.raises(ValueError):
        mod.make_client(Settings(anthropic_api_key=""))
    with pytest.raises(ValueError):
        mod.make_client(Settings(anthropic_api_key=None))
    with pytest.raises(ValueError):
        create_app(Settings(ocr_backend="anthropic", anthropic_api_key=""))

    # A non-empty key builds a client with no call out.
    assert mod.make_client(Settings(anthropic_api_key="test-key")) is not None


def test_ac5_dummy_key_builds_both_adapters_without_network(no_network: None) -> None:
    """Both collaborators are the real adapters, and construction alone calls nothing out."""
    mod = adapters()
    settings = Settings(
        ocr_backend="anthropic",
        extractor_backend="anthropic",
        anthropic_api_key="test-key",
        anthropic_model="claude-sonnet-5",
    )

    app = create_app(settings)

    assert isinstance(app.state.ocr_engine, mod.AnthropicOcrEngine)
    assert isinstance(app.state.field_extractor, mod.AnthropicFieldExtractor)
    # The app still answers a request that needs neither adapter.
    assert TestClient(app).get("/healthz").json() == {"status": "ok"}
    assert Settings().anthropic_model == "claude-sonnet-5"


def test_ac5_mixed_backends_build_one_adapter_and_one_fake(no_network: None) -> None:
    """Failure path: the two backends are chosen independently, not as a pair."""
    mod = adapters()

    app = create_app(
        Settings(ocr_backend="anthropic", extractor_backend="fake", anthropic_api_key="test-key")
    )

    assert isinstance(app.state.ocr_engine, mod.AnthropicOcrEngine)
    assert isinstance(app.state.field_extractor, extraction.FakeFieldExtractor)

    flipped = create_app(
        Settings(ocr_backend="fake", extractor_backend="anthropic", anthropic_api_key="test-key")
    )

    assert isinstance(flipped.state.ocr_engine, ocr.FakeOcrEngine)
    assert isinstance(flipped.state.field_extractor, mod.AnthropicFieldExtractor)


# --------------------------------------------------------------------------- AC6 (live)


@requires_api_key
def test_ac6_live_extraction_finds_all_four_fields() -> None:
    """Live: the real model maps licence prose onto the four fields (network, real key)."""
    mod = adapters()
    settings = Settings.from_env()
    extractor = mod.AnthropicFieldExtractor(mod.make_client(settings), settings.anthropic_model)

    result = extractor.extract_fields(LIVE_TEXT)
    validated = validation.validate_result(result, today=date.today())

    assert validated.missing_fields == []
    for name in models.FIELD_NAMES:
        assert getattr(validated, name).status == models.FieldStatus.FOUND, name
    assert validated.state.value == "CA"
    assert validated.expiration_date.value == "2099-12-31"
    assert validated.provider_name.value is not None
    assert validated.license_number.value is not None


@requires_sample_png
@requires_api_key
def test_ac6_live_ocr_returns_text_for_the_sample_png() -> None:
    """Live: vision OCR of the human-placed sample returns a non-empty transcription.

    Two preconditions, the key checked first: pytest reports the innermost marker, so the skip
    reason names the key while it is absent and the missing sample once a key is present.
    """
    mod = adapters()
    settings = Settings.from_env()
    engine = mod.AnthropicOcrEngine(mod.make_client(settings), settings.anthropic_model)

    text = engine.extract_text(SAMPLE_PNG.read_bytes(), "image/png")

    assert isinstance(text, str)
    assert text.strip() != ""
