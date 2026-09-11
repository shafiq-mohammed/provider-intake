"""Tests for T-003: text extraction behind the OcrEngine protocol.

Every app under test is built with create_app() and the OCR engine is injected; the
module-level `app` is never imported because it reads the process environment (SPEC A13 /
section 10). No network, no API key, no environment access.

The PDF fixtures below are inputs only (SPEC section 10): they build bytes, they never
reimplement routing, pdf parsing or the pdf-vs-ocr routing under test.
"""

import io
import json
from typing import Any

import pypdf
import pytest
from fastapi.testclient import TestClient

from app import models, ocr, text_extraction
from app.main import create_app
from app.repository import StoredDocument
from app.settings import Settings

# --------------------------------------------------------------------------- input fixtures

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_BYTES = PNG_SIGNATURE + b"\x00" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32

# Valid %PDF signature (so the T-002 upload check passes), unreadable body (pypdf raises).
CORRUPT_PDF_BYTES = b"%PDF-1.4 garbage"

# A marker that cannot occur incidentally in a response; used to prove that document bytes
# are never echoed back to the client (SPEC A17 / section 5).
SECRET_MARKER = "SECRETPAYLOADMARKER"
SECRET_PNG_BYTES = PNG_SIGNATURE + SECRET_MARKER.encode() + b"A" * 40

# The text layer of the AC1 document, and a deliberately thin (~60 char) one for AC2.
PDF_TEXT = "Provider: Jane Doe License: LCSW-12345 State: CA"
THIN_PDF_TEXT = "Provider Jane Doe License LCSW-12345 State CA Exp 2027 ok"

# A short, punctuation-free layer used to pin the >= boundary of min_pdf_text_chars (SPEC A6).
# Its measured length is asserted in the test rather than assumed here.
BOUNDARY_PDF_TEXT = "ABCDEFGHIJ"


def minimal_pdf(text: str) -> bytes:
    """Factory only: a one-page PDF whose text layer is exactly ``text`` (SPEC section 10).

    Avoid ``(``, ``)`` and ``\\`` inside ``text``.
    """
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def blank_pdf() -> bytes:
    """Factory only: a one-page PDF with an empty text layer (SPEC section 10)."""
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def encrypted_pdf() -> bytes:
    """Factory only: a one-page password-protected PDF (SPEC A6 names encrypted files)."""
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("pw")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def client_for(engine: Any, settings: Settings | None = None) -> TestClient:
    """Factory only: a client over an app with the injected OCR engine."""
    return TestClient(create_app(settings, ocr_engine=engine))


def upload(client: TestClient, name: str, data: bytes, content_type: str) -> str:
    """Factory only: upload one document through the real endpoint and return its id."""
    response = client.post("/documents", files={"file": (name, data, content_type)})
    assert response.status_code == 201, response.text
    document_id = response.json()["id"]
    assert isinstance(document_id, str)
    assert document_id != ""
    return document_id


def assert_envelope(body: Any, *, code: str, details: dict[str, Any] | None = None) -> None:
    """Assert the full error envelope shape key by key (SPEC section 9)."""
    assert isinstance(body, dict)
    assert list(body.keys()) == ["error"]
    error = body["error"]
    assert isinstance(error, dict)
    assert set(error.keys()) == {"code", "message", "details"}
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"] != ""
    assert isinstance(error["details"], dict)
    if details is not None:
        assert error["details"] == details


def assert_no_payload_leak(response: Any, marker: str) -> None:
    """Assert the marker appears nowhere in the response, in any encoding of the body."""
    assert marker not in response.text
    assert marker.encode() not in response.content
    assert marker not in json.dumps(response.json())


# --------------------------------------------------------------------------- AC1


def test_ac1_pdf_text_layer_is_used_and_the_engine_is_never_called() -> None:
    """Happy path: a PDF with a real text layer answers from pypdf, not from the engine."""
    engine = ocr.FakeOcrEngine("SHOULD NOT BE USED")
    client = client_for(engine)
    document_id = upload(client, "license.pdf", minimal_pdf(PDF_TEXT), "application/pdf")

    response = client.post(f"/documents/{document_id}/text")

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"document_id", "text", "source", "char_count"}
    assert body["document_id"] == document_id
    assert body["source"] == "pdf_text_layer"
    assert "Jane Doe" in body["text"]
    assert "LCSW-12345" in body["text"]
    assert body["char_count"] == len(body["text"])
    assert "SHOULD NOT BE USED" not in body["text"]
    assert engine.calls == []


def test_ac1_extract_text_called_directly_returns_the_same_text_extraction() -> None:
    """Edge: the function behind the endpoint returns the identical TextExtraction."""
    content = minimal_pdf(PDF_TEXT)
    engine = ocr.FakeOcrEngine("SHOULD NOT BE USED")
    client = client_for(ocr.FakeOcrEngine("SHOULD NOT BE USED"))
    document_id = upload(client, "license.pdf", content, "application/pdf")
    from_endpoint = client.post(f"/documents/{document_id}/text").json()

    document = StoredDocument(
        id=document_id,
        filename="license.pdf",
        content_type="application/pdf",
        content=content,
    )
    result = text_extraction.extract_text(document, engine, min_pdf_text_chars=20)

    assert isinstance(result, models.TextExtraction)
    assert result.source == "pdf_text_layer"
    assert result.text == from_endpoint["text"]
    assert result.char_count == len(result.text)
    assert result.char_count == from_endpoint["char_count"]
    assert engine.calls == []


def test_ac1_engine_calls_stay_empty_across_two_requests() -> None:
    """Failure path: repeating the request must not quietly fall through to the engine."""
    engine = ocr.FakeOcrEngine("SHOULD NOT BE USED")
    client = client_for(engine)
    document_id = upload(client, "license.pdf", minimal_pdf(PDF_TEXT), "application/pdf")

    first = client.post(f"/documents/{document_id}/text")
    second = client.post(f"/documents/{document_id}/text")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["source"] == "pdf_text_layer"
    assert engine.calls == []


# --------------------------------------------------------------------------- AC2


def test_ac2_blank_pdf_falls_back_to_the_ocr_engine() -> None:
    """Happy path: an empty text layer routes the PDF bytes to the engine."""
    engine = ocr.FakeOcrEngine("ocr text from vision")
    client = client_for(engine)
    document_id = upload(client, "scan.pdf", blank_pdf(), "application/pdf")

    response = client.post(f"/documents/{document_id}/text")

    assert response.status_code == 200
    body = response.json()
    assert body["document_id"] == document_id
    assert body["source"] == "ocr"
    assert body["text"] == "ocr text from vision"
    assert body["char_count"] == len("ocr text from vision")
    assert engine.calls == ["application/pdf"]


def test_ac2_text_layer_below_min_pdf_text_chars_uses_the_engine() -> None:
    """Edge: the fallback fires on length, not only on emptiness (SPEC A6)."""
    assert 20 <= len(THIN_PDF_TEXT) < 100
    content = minimal_pdf(THIN_PDF_TEXT)

    strict_engine = ocr.FakeOcrEngine("ocr text from vision")
    strict = client_for(strict_engine, Settings(min_pdf_text_chars=100))
    strict_id = upload(strict, "thin.pdf", content, "application/pdf")
    strict_response = strict.post(f"/documents/{strict_id}/text")

    assert strict_response.status_code == 200
    assert strict_response.json()["source"] == "ocr"
    assert strict_response.json()["text"] == "ocr text from vision"
    assert strict_engine.calls == ["application/pdf"]

    # Control: the very same document under the default threshold keeps its text layer.
    default_engine = ocr.FakeOcrEngine("ocr text from vision")
    default = client_for(default_engine)
    default_id = upload(default, "thin.pdf", content, "application/pdf")
    default_response = default.post(f"/documents/{default_id}/text")

    assert default_response.status_code == 200
    assert default_response.json()["source"] == "pdf_text_layer"
    assert "LCSW-12345" in default_response.json()["text"]
    assert default_engine.calls == []


def test_ac2_threshold_is_inclusive_at_exactly_min_pdf_text_chars() -> None:
    """Edge: both sides of the boundary, so `>=` cannot silently become `>` (SPEC A6).

    The exact length is measured from the fixture instead of assumed, because pypdf does
    not promise to hand back the input string byte for byte.
    """
    content = minimal_pdf(BOUNDARY_PDF_TEXT)
    exact = len(text_extraction.pdf_text_layer(content).strip())
    assert exact == 10

    # At the threshold: >= holds, the text layer wins and the engine is never touched.
    at_engine = ocr.FakeOcrEngine("ocr text from vision")
    at_client = client_for(at_engine, Settings(min_pdf_text_chars=exact))
    at_id = upload(at_client, "boundary.pdf", content, "application/pdf")
    at_response = at_client.post(f"/documents/{at_id}/text")

    assert at_response.status_code == 200
    assert at_response.json()["source"] == "pdf_text_layer"
    assert BOUNDARY_PDF_TEXT in at_response.json()["text"]
    assert at_engine.calls == []

    # One character above it: the same bytes now fall through to the engine.
    above_engine = ocr.FakeOcrEngine("ocr text from vision")
    above_client = client_for(above_engine, Settings(min_pdf_text_chars=exact + 1))
    above_id = upload(above_client, "boundary.pdf", content, "application/pdf")
    above_response = above_client.post(f"/documents/{above_id}/text")

    assert above_response.status_code == 200
    assert above_response.json()["source"] == "ocr"
    assert above_response.json()["text"] == "ocr text from vision"
    assert above_engine.calls == ["application/pdf"]


def test_ac2_engine_returning_empty_text_still_returns_200() -> None:
    """Failure path: an engine that finds nothing is a 200 with an empty string, not an error."""
    engine = ocr.FakeOcrEngine("")
    client = client_for(engine)
    document_id = upload(client, "scan.pdf", blank_pdf(), "application/pdf")

    response = client.post(f"/documents/{document_id}/text")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "ocr"
    assert body["text"] == ""
    assert body["char_count"] == 0
    assert engine.calls == ["application/pdf"]


# --------------------------------------------------------------------------- AC3


def test_ac3_png_is_sent_to_the_engine_with_its_content_type() -> None:
    """Happy path: an image always goes to the engine, and its bytes never come back."""
    engine = ocr.FakeOcrEngine("png text")
    client = client_for(engine)
    document_id = upload(client, "license.png", SECRET_PNG_BYTES, "image/png")

    response = client.post(f"/documents/{document_id}/text")

    assert response.status_code == 200
    body = response.json()
    assert body["document_id"] == document_id
    assert body["source"] == "ocr"
    assert body["text"] == "png text"
    assert body["char_count"] == 8
    assert engine.calls == ["image/png"]
    assert_no_payload_leak(response, SECRET_MARKER)


def test_ac3_jpeg_is_sent_to_the_engine_with_its_content_type() -> None:
    """Edge: the other image type reaches the engine under its own declared type."""
    engine = ocr.FakeOcrEngine("jpeg text")
    client = client_for(engine)
    document_id = upload(client, "license.jpg", JPEG_BYTES, "image/jpeg")

    response = client.post(f"/documents/{document_id}/text")

    assert response.status_code == 200
    assert response.json()["source"] == "ocr"
    assert response.json()["text"] == "jpeg text"
    assert engine.calls == ["image/jpeg"]


def test_ac3_repeated_requests_call_the_engine_again_without_caching() -> None:
    """Failure path: results are recomputed every call (SPEC section 2, A5)."""
    engine = ocr.FakeOcrEngine("png text")
    client = client_for(engine)
    document_id = upload(client, "license.png", PNG_BYTES, "image/png")

    first = client.post(f"/documents/{document_id}/text")
    second = client.post(f"/documents/{document_id}/text")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert engine.calls == ["image/png", "image/png"]


# --------------------------------------------------------------------------- AC4


def test_ac4_engine_failure_returns_422_ocr_failed_envelope() -> None:
    """Happy path for the error contract: an engine exception is a 422, never a 500."""
    engine = ocr.FakeOcrEngine(error=RuntimeError("vision down"))
    client = client_for(engine)
    document_id = upload(client, "license.png", SECRET_PNG_BYTES, "image/png")

    response = client.post(f"/documents/{document_id}/text")

    assert response.status_code == 422
    assert_envelope(response.json(), code="ocr_failed", details={"reason": "RuntimeError"})
    assert_no_payload_leak(response, SECRET_MARKER)
    assert "PNG" not in response.text
    assert engine.calls == ["image/png"]


def test_ac4_custom_exception_class_name_is_the_reported_reason() -> None:
    """Edge: the reason is the engine exception's class name, whatever that class is."""

    class VisionDown(Exception):
        pass

    engine = ocr.FakeOcrEngine(error=VisionDown("upstream 503"))
    client = client_for(engine)
    document_id = upload(client, "license.png", SECRET_PNG_BYTES, "image/png")

    response = client.post(f"/documents/{document_id}/text")

    assert response.status_code == 422
    assert_envelope(response.json(), code="ocr_failed", details={"reason": "VisionDown"})
    assert_no_payload_leak(response, SECRET_MARKER)


def test_ac4_engine_exception_message_never_reaches_the_client() -> None:
    """Failure path: the reason is the class name only, so the message is dropped (SPEC A17).

    Two legs: a message that quotes the payload, and an exception built straight from the
    raw document bytes. Neither may surface in any encoding of the response body.
    """
    message_engine = ocr.FakeOcrEngine(
        error=RuntimeError(f"vision down while reading {SECRET_MARKER}")
    )
    message_client = client_for(message_engine)
    message_id = upload(message_client, "license.png", PNG_BYTES, "image/png")

    message_response = message_client.post(f"/documents/{message_id}/text")

    assert message_response.status_code == 422
    assert_envelope(message_response.json(), code="ocr_failed", details={"reason": "RuntimeError"})
    assert_no_payload_leak(message_response, SECRET_MARKER)
    assert "vision down" not in message_response.text
    assert message_engine.calls == ["image/png"]

    # Second leg: the exception is constructed from the document bytes themselves.
    bytes_engine = ocr.FakeOcrEngine(error=ValueError(SECRET_PNG_BYTES))
    bytes_client = client_for(bytes_engine)
    bytes_id = upload(bytes_client, "license.png", SECRET_PNG_BYTES, "image/png")

    bytes_response = bytes_client.post(f"/documents/{bytes_id}/text")

    assert bytes_response.status_code == 422
    assert_envelope(bytes_response.json(), code="ocr_failed", details={"reason": "ValueError"})
    assert_no_payload_leak(bytes_response, SECRET_MARKER)
    assert bytes_engine.calls == ["image/png"]


def test_ac4_extract_text_raises_ocr_error_wrapping_the_original() -> None:
    """Failure path: the function raises OcrError with the engine's exception as __cause__."""
    original = RuntimeError("vision down")
    engine = ocr.FakeOcrEngine(error=original)
    document = StoredDocument(
        id="doc-1",
        filename="license.png",
        content_type="image/png",
        content=SECRET_PNG_BYTES,
    )

    with pytest.raises(ocr.OcrError) as excinfo:
        text_extraction.extract_text(document, engine, min_pdf_text_chars=20)

    assert excinfo.value.__cause__ is original
    assert type(excinfo.value.__cause__).__name__ == "RuntimeError"
    assert SECRET_MARKER not in str(excinfo.value)
    assert engine.calls == ["image/png"]


# --------------------------------------------------------------------------- AC5


def test_ac5_unknown_document_id_returns_404_document_not_found() -> None:
    """Happy path: an app built with no engine at all still answers 404 for a missing id."""
    client = TestClient(create_app())

    response = client.post("/documents/missing/text")

    assert response.status_code == 404
    assert_envelope(response.json(), code="document_not_found", details={"document_id": "missing"})


def test_ac5_get_on_the_text_endpoint_returns_405_envelope() -> None:
    """Edge: /text is POST-only (SPEC A5); GET on a valid id is the 405 envelope."""
    client = client_for(ocr.FakeOcrEngine("png text"))
    document_id = upload(client, "license.png", PNG_BYTES, "image/png")

    response = client.get(f"/documents/{document_id}/text")

    assert response.status_code == 405
    assert_envelope(response.json(), code="method_not_allowed", details={})


def test_ac5_build_ocr_engine_defaults_to_fake_and_rejects_anthropic() -> None:
    """Failure path: any backend other than 'fake' is a ValueError until T-007."""
    engine = ocr.build_ocr_engine(Settings())

    assert isinstance(engine, ocr.FakeOcrEngine)
    assert engine.calls == []

    with pytest.raises(ValueError):
        ocr.build_ocr_engine(Settings(ocr_backend="anthropic"))

    with pytest.raises(ValueError):
        create_app(Settings(ocr_backend="anthropic"))


def test_ac5_injected_engine_is_stored_on_app_state() -> None:
    """Edge: the injected collaborator is the one the app keeps (SPEC section 5)."""
    engine = ocr.FakeOcrEngine("png text")

    app = create_app(ocr_engine=engine)

    assert app.state.ocr_engine is engine
    assert isinstance(create_app().state.ocr_engine, ocr.FakeOcrEngine)


# --------------------------------------------------------------------------- AC6


def test_ac6_corrupt_pdf_falls_back_to_the_engine() -> None:
    """Happy path: a PDF pypdf cannot read is treated as an empty text layer (SPEC A6)."""
    engine = ocr.FakeOcrEngine("fallback")
    client = client_for(engine)
    document_id = upload(client, "broken.pdf", CORRUPT_PDF_BYTES, "application/pdf")

    response = client.post(f"/documents/{document_id}/text")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "ocr"
    assert body["text"] == "fallback"
    assert body["char_count"] == len("fallback")
    assert engine.calls == ["application/pdf"]
    assert "garbage" not in response.text


def test_ac6_pdf_text_layer_returns_empty_string_for_unreadable_and_blank_pdfs() -> None:
    """Edge: pdf_text_layer swallows pypdf failures and reports an empty layer.

    Encrypted files are named by the spec docstring alongside corrupt ones: they must
    yield "" rather than raise, so the routing falls through to the engine (SPEC A6).
    """
    assert text_extraction.pdf_text_layer(CORRUPT_PDF_BYTES) == ""
    assert text_extraction.pdf_text_layer(blank_pdf()) == ""
    assert text_extraction.pdf_text_layer(b"") == ""
    assert text_extraction.pdf_text_layer(encrypted_pdf()) == ""
    # Positive control: a readable PDF really does yield its text layer.
    assert "Jane Doe" in text_extraction.pdf_text_layer(minimal_pdf(PDF_TEXT))


def test_ac6_corrupt_pdf_with_a_failing_engine_is_422_never_500() -> None:
    """Failure path: unreadable PDF plus a broken engine is still the envelope (SPEC A16)."""
    engine = ocr.FakeOcrEngine(error=RuntimeError("vision down"))
    client = client_for(engine)
    document_id = upload(client, "broken.pdf", CORRUPT_PDF_BYTES, "application/pdf")

    response = client.post(f"/documents/{document_id}/text")

    assert response.status_code == 422
    assert_envelope(response.json(), code="ocr_failed", details={"reason": "RuntimeError"})
    assert "garbage" not in response.text
    assert engine.calls == ["application/pdf"]
