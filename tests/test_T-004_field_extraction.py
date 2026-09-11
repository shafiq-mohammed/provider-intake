"""Tests for T-004: LLM field extraction and the per-field uncertainty model.

Every app under test is built with create_app() with both collaborators injected; the
module-level `app` is never imported because it reads the process environment (SPEC A13 /
section 10). No network, no API key, no environment access.

Field values are deliberately canonical (two-letter state code, ISO date far in the future)
so that the deterministic validation step T-005 inserts into this same endpoint leaves every
`found` result here unchanged and these tests keep passing (SPEC section 11, T-005).

The PDF and PNG factories below are inputs only (SPEC section 10): they build bytes, they
never reimplement routing, extraction or the value-nulling rule under test.
"""

import json
from typing import Any

import pydantic
import pytest
from fastapi.testclient import TestClient

from app import extraction, models, ocr
from app.main import create_app
from app.settings import Settings

# --------------------------------------------------------------------------- input fixtures

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# A marker that cannot occur incidentally in a response; used to prove that document bytes
# are never echoed back to the client, on success and on every error path (SPEC A17).
SECRET_MARKER = "SECRETPAYLOADMARKER"
SECRET_PNG_BYTES = PNG_SIGNATURE + SECRET_MARKER.encode() + b"A" * 40

# The canonical all-found values. A 2-letter state code and an ISO date far in the future
# both survive T-005 validation untouched.
CANONICAL_PROVIDER_NAME = "Jane Doe"
CANONICAL_LICENSE_NUMBER = "LCSW-12345"
CANONICAL_STATE = "CA"
CANONICAL_EXPIRATION_DATE = "2099-12-31"

OCR_TEXT = "some text"

# A text layer long enough to beat the default min_pdf_text_chars of 20 (SPEC A6).
PDF_TEXT = "Provider: Jane Doe License: LCSW-12345 State: CA"


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


def make_result(**overrides: Any) -> Any:
    """Factory only: the all-found canonical ExtractionResult, with fields replaced.

    Each override is a ``FieldResult`` for one of ``FIELD_NAMES``. Nothing here computes
    ``missing_fields`` or nulls a value; that is the behavior under test.
    """
    fields: dict[str, Any] = {
        "provider_name": models.FieldResult(
            value=CANONICAL_PROVIDER_NAME,
            status="found",
            raw=CANONICAL_PROVIDER_NAME,
        ),
        "license_number": models.FieldResult(
            value=CANONICAL_LICENSE_NUMBER,
            status="found",
            raw=CANONICAL_LICENSE_NUMBER,
        ),
        "state": models.FieldResult(
            value=CANONICAL_STATE,
            status="found",
            raw=CANONICAL_STATE,
        ),
        "expiration_date": models.FieldResult(
            value=CANONICAL_EXPIRATION_DATE,
            status="found",
            raw=CANONICAL_EXPIRATION_DATE,
        ),
    }
    unknown = set(overrides) - set(fields)
    assert unknown == set(), f"make_result got unknown field names: {sorted(unknown)}"
    fields.update(overrides)
    return models.ExtractionResult(**fields)


def client_for(engine: Any, extractor: Any = None, settings: Settings | None = None) -> TestClient:
    """Factory only: a client over an app with both collaborators injected."""
    return TestClient(create_app(settings, ocr_engine=engine, field_extractor=extractor))


def upload_png(client: TestClient) -> str:
    """Factory only: upload one PNG through the real endpoint and return its id.

    The bytes carry SECRET_MARKER so that any test can assert the payload never comes back.
    """
    response = client.post(
        "/documents", files={"file": ("license.png", SECRET_PNG_BYTES, "image/png")}
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["id"]
    assert isinstance(document_id, str)
    assert document_id != ""
    return document_id


def upload_pdf(client: TestClient, content: bytes) -> str:
    """Factory only: upload one PDF through the real endpoint and return its id."""
    response = client.post(
        "/documents", files={"file": ("license.pdf", content, "application/pdf")}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


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


def assert_response_shape(body: Any) -> None:
    """Assert the ExtractionResponse envelope carries exactly the documented keys."""
    assert isinstance(body, dict)
    assert set(body.keys()) == {
        "document_id",
        "text_source",
        "provider_name",
        "license_number",
        "state",
        "expiration_date",
        "missing_fields",
    }
    for name in ("provider_name", "license_number", "state", "expiration_date"):
        assert set(body[name].keys()) == {"value", "status", "raw", "issues"}


# --------------------------------------------------------------------------- AC1


def test_ac1_all_fields_found_returns_200_with_every_field() -> None:
    """Happy path: all four canonical fields found, nothing missing, extractor saw the text."""
    engine = ocr.FakeOcrEngine(OCR_TEXT)
    extractor = extraction.FakeFieldExtractor(make_result())
    client = client_for(engine, extractor)
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 200
    body = response.json()
    assert_response_shape(body)
    assert body["document_id"] == document_id
    assert body["text_source"] == "ocr"
    assert body["provider_name"] == {
        "value": CANONICAL_PROVIDER_NAME,
        "status": "found",
        "raw": CANONICAL_PROVIDER_NAME,
        "issues": [],
    }
    assert body["license_number"] == {
        "value": CANONICAL_LICENSE_NUMBER,
        "status": "found",
        "raw": CANONICAL_LICENSE_NUMBER,
        "issues": [],
    }
    assert body["state"] == {
        "value": CANONICAL_STATE,
        "status": "found",
        "raw": CANONICAL_STATE,
        "issues": [],
    }
    assert body["expiration_date"] == {
        "value": CANONICAL_EXPIRATION_DATE,
        "status": "found",
        "raw": CANONICAL_EXPIRATION_DATE,
        "issues": [],
    }
    assert body["missing_fields"] == []
    assert extractor.calls == [OCR_TEXT]
    assert engine.calls == ["image/png"]
    assert_no_payload_leak(response, SECRET_MARKER)


def test_ac1_pdf_text_layer_is_the_text_source_and_feeds_the_extractor() -> None:
    """Edge: a PDF answered from its text layer reports that source and skips the engine."""
    engine = ocr.FakeOcrEngine("SHOULD NOT BE USED")
    extractor = extraction.FakeFieldExtractor(make_result())
    client = client_for(engine, extractor)
    document_id = upload_pdf(client, minimal_pdf(PDF_TEXT))

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 200
    body = response.json()
    assert body["text_source"] == "pdf_text_layer"
    assert body["missing_fields"] == []
    assert engine.calls == []
    assert len(extractor.calls) == 1
    assert "Jane Doe" in extractor.calls[0]
    assert "LCSW-12345" in extractor.calls[0]
    assert "SHOULD NOT BE USED" not in extractor.calls[0]


def test_ac1_unknown_document_id_returns_404_document_not_found() -> None:
    """Failure path: an unknown id is the 404 envelope and the extractor is never called."""
    engine = ocr.FakeOcrEngine(OCR_TEXT)
    extractor = extraction.FakeFieldExtractor(make_result())
    client = client_for(engine, extractor)

    response = client.post("/documents/missing/extract")

    assert response.status_code == 404
    assert_envelope(response.json(), code="document_not_found", details={"document_id": "missing"})
    assert extractor.calls == []
    assert engine.calls == []


# --------------------------------------------------------------------------- AC2


def test_ac2_not_found_field_is_null_and_listed_in_missing_fields() -> None:
    """Happy path: a field the document does not carry is null with status not_found."""
    engine = ocr.FakeOcrEngine(OCR_TEXT)
    extractor = extraction.FakeFieldExtractor(
        make_result(state=models.FieldResult(status="not_found"))
    )
    client = client_for(engine, extractor)
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 200
    body = response.json()
    assert_response_shape(body)
    assert body["state"] == {"value": None, "status": "not_found", "raw": None, "issues": []}
    assert body["missing_fields"] == ["state"]
    assert body["provider_name"]["value"] == CANONICAL_PROVIDER_NAME
    assert body["license_number"]["value"] == CANONICAL_LICENSE_NUMBER
    assert body["expiration_date"]["value"] == CANONICAL_EXPIRATION_DATE
    assert extractor.calls == [OCR_TEXT]


def test_ac2_two_missing_fields_are_listed_in_field_names_order() -> None:
    """Edge: missing_fields follows FIELD_NAMES, not the order the fields were built in."""
    assert models.FIELD_NAMES == (
        "provider_name",
        "license_number",
        "state",
        "expiration_date",
    )
    # expiration_date is supplied first on purpose; the answer must still be FIELD_NAMES order.
    result = models.ExtractionResult(
        expiration_date=models.FieldResult(status="not_found"),
        provider_name=models.FieldResult(status="not_found"),
        license_number=models.FieldResult(
            value=CANONICAL_LICENSE_NUMBER, status="found", raw=CANONICAL_LICENSE_NUMBER
        ),
        state=models.FieldResult(value=CANONICAL_STATE, status="found", raw=CANONICAL_STATE),
    )
    engine = ocr.FakeOcrEngine(OCR_TEXT)
    extractor = extraction.FakeFieldExtractor(result)
    client = client_for(engine, extractor)
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 200
    assert response.json()["missing_fields"] == ["provider_name", "expiration_date"]
    assert result.missing_fields == ["provider_name", "expiration_date"]


def test_ac2_not_found_field_result_serializes_with_value_null() -> None:
    """Failure path: a value handed in with a non-found status is nulled, never rejected.

    SPEC A8 is enforced by the model itself, so the leak cannot reach the wire even when the
    extractor tries to attach a value to a not_found field.
    """
    leaky = models.FieldResult(status="not_found", value="leak")

    assert leaky.value is None
    assert leaky.model_dump()["value"] is None
    assert "leak" not in json.dumps(leaky.model_dump(mode="json"))

    # The same invariant, observed through the endpoint.
    engine = ocr.FakeOcrEngine(OCR_TEXT)
    extractor = extraction.FakeFieldExtractor(
        make_result(state=models.FieldResult(status="not_found", value="leak"))
    )
    client = client_for(engine, extractor)
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 200
    assert response.json()["state"]["value"] is None
    assert response.json()["state"]["status"] == "not_found"
    assert response.json()["missing_fields"] == ["state"]
    assert "leak" not in response.text


# --------------------------------------------------------------------------- AC3


def test_ac3_unreadable_field_keeps_raw_and_issues() -> None:
    """Happy path: a field the model could not read keeps its raw text and issue codes."""
    engine = ocr.FakeOcrEngine(OCR_TEXT)
    extractor = extraction.FakeFieldExtractor(
        make_result(
            expiration_date=models.FieldResult(
                status="unreadable", raw="12/3?/2027", issues=["smudged"]
            )
        )
    )
    client = client_for(engine, extractor)
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 200
    body = response.json()
    assert_response_shape(body)
    assert body["expiration_date"] == {
        "value": None,
        "status": "unreadable",
        "raw": "12/3?/2027",
        "issues": ["smudged"],
    }
    assert body["missing_fields"] == ["expiration_date"]
    assert body["provider_name"]["status"] == "found"
    assert extractor.calls == [OCR_TEXT]


def test_ac3_unreadable_field_with_no_raw_and_no_issues() -> None:
    """Edge: raw and issues are optional; an unreadable field with neither is still 200."""
    engine = ocr.FakeOcrEngine(OCR_TEXT)
    extractor = extraction.FakeFieldExtractor(
        make_result(expiration_date=models.FieldResult(status="unreadable"))
    )
    client = client_for(engine, extractor)
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 200
    body = response.json()
    assert body["expiration_date"] == {
        "value": None,
        "status": "unreadable",
        "raw": None,
        "issues": [],
    }
    assert body["missing_fields"] == ["expiration_date"]


def test_ac3_unknown_status_string_is_rejected_by_field_result() -> None:
    """Failure path: a status outside FieldStatus is a ValidationError, not a silent pass."""
    with pytest.raises(pydantic.ValidationError):
        models.FieldResult(status="bogus")

    with pytest.raises(pydantic.ValidationError):
        models.FieldResult(value="x", status="FOUND")

    # Control: every member of the enum is accepted by its string value.
    for status in ("found", "not_found", "unreadable", "invalid"):
        assert models.FieldResult(status=status).status == status


# --------------------------------------------------------------------------- AC4


def test_ac4_extractor_error_returns_422_extraction_failed() -> None:
    """Happy path for the error contract: an extractor exception is a 422, never a 500."""
    engine = ocr.FakeOcrEngine(OCR_TEXT)
    extractor = extraction.FakeFieldExtractor(error=RuntimeError("llm down"))
    client = client_for(engine, extractor)
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 422
    assert_envelope(response.json(), code="extraction_failed", details={"reason": "RuntimeError"})
    assert_no_payload_leak(response, SECRET_MARKER)
    assert "llm down" not in response.text
    assert extractor.calls == [OCR_TEXT]


def test_ac4_custom_exception_class_name_is_the_reported_reason() -> None:
    """Edge: the reason is the extractor exception's class name, whatever that class is.

    The exception message quotes the payload, so the response also pins SPEC A17: only the
    class name may cross the wire (a leak of exactly this shape was found in T-002 review).
    """

    class LlmUnavailable(Exception):
        pass

    engine = ocr.FakeOcrEngine(OCR_TEXT)
    extractor = extraction.FakeFieldExtractor(
        error=LlmUnavailable(f"upstream 503 while reading {SECRET_MARKER}")
    )
    client = client_for(engine, extractor)
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 422
    assert_envelope(response.json(), code="extraction_failed", details={"reason": "LlmUnavailable"})
    assert_no_payload_leak(response, SECRET_MARKER)
    assert "upstream 503" not in response.text
    assert extractor.calls == [OCR_TEXT]


def test_ac4_ocr_failure_short_circuits_before_the_extractor() -> None:
    """Failure path: an OCR error is 422 ocr_failed and the extractor is never reached."""
    engine = ocr.FakeOcrEngine(error=RuntimeError("vision down"))
    extractor = extraction.FakeFieldExtractor(make_result())
    client = client_for(engine, extractor)
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 422
    assert_envelope(response.json(), code="ocr_failed", details={"reason": "RuntimeError"})
    assert_no_payload_leak(response, SECRET_MARKER)
    assert "vision down" not in response.text
    assert engine.calls == ["image/png"]
    assert extractor.calls == []


# --------------------------------------------------------------------------- AC5


def test_ac5_whitespace_only_text_short_circuits_with_no_text() -> None:
    """Happy path: blank OCR text means every field not_found with no_text, no LLM call."""
    engine = ocr.FakeOcrEngine("   \n")
    extractor = extraction.FakeFieldExtractor(make_result())
    client = client_for(engine, extractor)
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 200
    body = response.json()
    assert_response_shape(body)
    assert body["document_id"] == document_id
    assert body["text_source"] == "ocr"
    for name in models.FIELD_NAMES:
        assert body[name] == {
            "value": None,
            "status": "not_found",
            "raw": None,
            "issues": ["no_text"],
        }
    assert body["missing_fields"] == [
        "provider_name",
        "license_number",
        "state",
        "expiration_date",
    ]
    assert body["missing_fields"] == list(models.FIELD_NAMES)
    assert extractor.calls == []
    assert engine.calls == ["image/png"]
    assert CANONICAL_PROVIDER_NAME not in response.text


def test_ac5_empty_text_short_circuits_with_no_text() -> None:
    """Edge: exactly "" takes the same short-circuit as whitespace."""
    engine = ocr.FakeOcrEngine("")
    extractor = extraction.FakeFieldExtractor(make_result())
    client = client_for(engine, extractor)
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 200
    body = response.json()
    assert body["missing_fields"] == list(models.FIELD_NAMES)
    assert body["state"] == {
        "value": None,
        "status": "not_found",
        "raw": None,
        "issues": ["no_text"],
    }
    assert extractor.calls == []


def test_ac5_non_blank_text_does_call_the_extractor() -> None:
    """Failure path: the short-circuit must not swallow real text; "x" reaches the LLM."""
    engine = ocr.FakeOcrEngine("x")
    extractor = extraction.FakeFieldExtractor(make_result())
    client = client_for(engine, extractor)
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 200
    body = response.json()
    assert extractor.calls == ["x"]
    assert body["missing_fields"] == []
    assert body["provider_name"]["value"] == CANONICAL_PROVIDER_NAME
    assert body["provider_name"]["issues"] == []


# --------------------------------------------------------------------------- AC6


def test_ac6_field_result_nulls_value_and_status_rank_is_ordered() -> None:
    """Happy path: the direct model contract, value nulling plus the certainty ranking."""
    assert models.FieldResult(value="x", status="invalid").value is None
    assert models.FieldResult(value="x", status="unreadable").value is None
    assert models.FieldResult(value="x", status="not_found").value is None
    # Control: a found field keeps its value.
    assert models.FieldResult(value="x", status="found").value == "x"

    assert (
        models.FieldStatus.FOUND.rank
        > models.FieldStatus.INVALID.rank
        > models.FieldStatus.UNREADABLE.rank
        > models.FieldStatus.NOT_FOUND.rank
    )
    assert models.FieldStatus.FOUND.rank == 3
    assert models.FieldStatus.INVALID.rank == 2
    assert models.FieldStatus.UNREADABLE.rank == 1
    assert models.FieldStatus.NOT_FOUND.rank == 0
    assert models.FieldResult(status="found", value="A").model_dump()["status"] == "found"
    assert models.FieldResult(status="found", value="A").model_dump(mode="json") == {
        "value": "A",
        "status": "found",
        "raw": None,
        "issues": [],
    }


def test_ac6_all_with_and_fields_follow_field_names_order() -> None:
    """Edge: all_with() builds every field, fields() keys are exactly FIELD_NAMES in order."""
    result = models.ExtractionResult.all_with(models.FieldStatus.NOT_FOUND)

    dumped = result.model_dump()
    assert dumped["missing_fields"] == [
        "provider_name",
        "license_number",
        "state",
        "expiration_date",
    ]
    assert list(result.fields().keys()) == list(models.FIELD_NAMES)
    assert all(isinstance(field, models.FieldResult) for field in result.fields().values())
    assert [field.status for field in result.fields().values()] == [
        models.FieldStatus.NOT_FOUND
    ] * 4

    with_issues = models.ExtractionResult.all_with(models.FieldStatus.NOT_FOUND, issues=["no_text"])
    assert [field.issues for field in with_issues.fields().values()] == [["no_text"]] * 4
    assert with_issues.model_dump(mode="json")["provider_name"] == {
        "value": None,
        "status": "not_found",
        "raw": None,
        "issues": ["no_text"],
    }
    # The all-found canonical result has nothing missing and keeps FIELD_NAMES ordering.
    assert make_result().missing_fields == []
    assert list(make_result().fields().keys()) == list(models.FIELD_NAMES)


def test_ac6_build_field_extractor_defaults_to_fake_and_rejects_anthropic() -> None:
    """Failure path: any backend other than 'fake' is a ValueError until T-007."""
    built = extraction.build_field_extractor(Settings())

    assert isinstance(built, extraction.FakeFieldExtractor)
    assert built.calls == []
    assert built.extract_fields("anything").missing_fields == list(models.FIELD_NAMES)
    assert built.calls == ["anything"]

    with pytest.raises(ValueError):
        extraction.build_field_extractor(Settings(extractor_backend="anthropic"))

    with pytest.raises(ValueError):
        create_app(Settings(extractor_backend="anthropic"))


def test_ac6_injected_extractor_is_stored_on_app_state() -> None:
    """Edge: the injected collaborator is the one the app keeps (SPEC section 5)."""
    extractor = extraction.FakeFieldExtractor(make_result())

    app = create_app(field_extractor=extractor)

    assert app.state.field_extractor is extractor
    assert isinstance(create_app().state.field_extractor, extraction.FakeFieldExtractor)
