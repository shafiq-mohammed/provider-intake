"""Tests for T-002: upload endpoint, file validation, in-memory document repository.

Every app under test is built with create_app(); the module-level `app` is never imported
because it reads the process environment (SPEC A13 / section 10). The repository is injected
so each test owns its storage. No network, no API key.
"""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import models, repository, uploads
from app.errors import ApiError
from app.main import create_app
from app.settings import Settings

# --------------------------------------------------------------------------- input fixtures
# Inputs only (SPEC section 10): these bytes are never used to reproduce behavior under test.

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_BYTES = PNG_SIGNATURE + b"\x00" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PDF_BYTES = b"%PDF-1.4\n" + b"%" * 32

# A marker that cannot occur incidentally in an error envelope; used to prove that uploaded
# document bytes are never echoed back to the client (SPEC A17 / section 5).
SECRET_MARKER = "SECRETPAYLOADMARKER"
SECRET_PNG_BYTES = PNG_SIGNATURE + SECRET_MARKER.encode() + b"A" * 40

ALLOWED = ["application/pdf", "image/jpeg", "image/png"]


def png_of_size(size: int) -> bytes:
    """Build a signature-valid PNG padded to exactly ``size`` bytes."""
    return PNG_SIGNATURE + b"\x00" * (size - len(PNG_SIGNATURE))


@pytest.fixture
def repo() -> repository.InMemoryDocumentRepository:
    """Factory only: a fresh in-memory repository per test."""
    return repository.InMemoryDocumentRepository()


@pytest.fixture
def client(repo: repository.InMemoryDocumentRepository) -> TestClient:
    """Factory only: a client over an app with the injected repository."""
    return TestClient(create_app(repository=repo))


def client_with(settings: Settings) -> TestClient:
    """Factory only: a client over an app built from explicit settings."""
    return TestClient(create_app(settings))


def upload(client: TestClient, name: str, data: bytes, content_type: str) -> Any:  # httpx.Response
    """Factory only: posts one multipart part named `file` (SPEC section 10)."""
    return client.post("/documents", files={"file": (name, data, content_type)})


def assert_envelope(body: Any, *, code: str, details: dict[str, Any] | None = None) -> None:
    """Assert the full error envelope shape key by key."""
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


def test_ac1_png_upload_returns_201_metadata_and_is_retrievable(
    client: TestClient, repo: repository.InMemoryDocumentRepository
) -> None:
    """Happy path: a PNG is stored, its metadata returned, and fetched back unchanged."""
    response = upload(client, "license.png", PNG_BYTES, "image/png")

    assert response.status_code == 201
    body = response.json()
    assert set(body.keys()) == {"id", "filename", "content_type", "size_bytes"}
    assert isinstance(body["id"], str)
    assert body["id"] != ""
    assert body["filename"] == "license.png"
    assert body["content_type"] == "image/png"
    assert body["size_bytes"] == len(PNG_BYTES)
    assert models.DocumentMeta.model_validate(body).model_dump() == body

    fetched = client.get(f"/documents/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == body

    assert len(repo) == 1
    stored = repo.get(body["id"])
    assert stored is not None
    assert stored.content == PNG_BYTES
    assert stored.size_bytes == len(PNG_BYTES)
    assert stored.meta().model_dump() == body


def test_ac1_stored_document_repr_hides_the_file_bytes(
    client: TestClient, repo: repository.InMemoryDocumentRepository
) -> None:
    """Document bytes never appear in repr()/logs (SPEC section 5)."""
    document_id = upload(client, "license.png", PNG_BYTES, "image/png").json()["id"]

    stored_repr = repr(repo.get(document_id))

    assert document_id in stored_repr
    assert repr(PNG_BYTES) not in stored_repr
    assert "\\x89PNG" not in stored_repr
    assert "\\x00" not in stored_repr


def test_ac1_jpeg_and_pdf_uploads_return_201_with_their_content_type(
    client: TestClient, repo: repository.InMemoryDocumentRepository
) -> None:
    """Edge: the other two accepted media types round-trip just like PNG."""
    jpeg = upload(client, "license.jpg", JPEG_BYTES, "image/jpeg")
    assert jpeg.status_code == 201
    assert jpeg.json()["content_type"] == "image/jpeg"
    assert jpeg.json()["size_bytes"] == len(JPEG_BYTES)

    pdf = upload(client, "license.pdf", PDF_BYTES, "application/pdf")
    assert pdf.status_code == 201
    assert pdf.json()["content_type"] == "application/pdf"
    assert pdf.json()["size_bytes"] == len(PDF_BYTES)

    assert len(repo) == 2


def test_ac1_two_uploads_get_distinct_ids_and_do_not_overwrite(
    client: TestClient, repo: repository.InMemoryDocumentRepository
) -> None:
    """Failure path: a second upload must not clobber the first one's id or bytes."""
    first = upload(client, "a.png", PNG_BYTES, "image/png").json()
    second = upload(client, "b.pdf", PDF_BYTES, "application/pdf").json()

    assert first["id"] != second["id"]
    assert len(repo) == 2
    assert client.get(f"/documents/{first['id']}").json() == first
    assert client.get(f"/documents/{second['id']}").json() == second
    assert repo.get(first["id"]).content == PNG_BYTES
    assert repo.get(second["id"]).content == PDF_BYTES


# --------------------------------------------------------------------------- AC2


def test_ac2_one_byte_over_the_limit_returns_413_envelope() -> None:
    """Happy path: 65 bytes against a 64 byte limit is file_too_large."""
    client = client_with(Settings(max_upload_bytes=64))

    response = upload(client, "big.png", png_of_size(65), "image/png")

    assert response.status_code == 413
    assert_envelope(
        response.json(),
        code="file_too_large",
        details={"max_upload_bytes": 64, "size_bytes": 65},
    )


def test_ac2_exactly_max_upload_bytes_is_accepted() -> None:
    """Edge: the boundary value itself is accepted with 201."""
    client = client_with(Settings(max_upload_bytes=64))

    response = upload(client, "exact.png", png_of_size(64), "image/png")

    assert response.status_code == 201
    assert response.json()["size_bytes"] == 64


def test_ac2_far_oversized_payload_is_413_envelope_not_500() -> None:
    """Failure path: a much larger body is still the envelope, never a 500."""
    client = client_with(Settings(max_upload_bytes=64))
    oversized = png_of_size(64 + 1000)

    response = upload(client, "huge.png", oversized, "image/png")

    assert response.status_code == 413
    assert_envelope(
        response.json(),
        code="file_too_large",
        details={"max_upload_bytes": 64, "size_bytes": 1064},
    )


# --------------------------------------------------------------------------- AC3


def test_ac3_disallowed_content_type_returns_415_unsupported_media_type(
    client: TestClient, repo: repository.InMemoryDocumentRepository
) -> None:
    """Happy path: text/plain is rejected before anything is stored."""
    response = upload(client, "notes.txt", b"hello", "text/plain")

    assert response.status_code == 415
    assert_envelope(
        response.json(),
        code="unsupported_media_type",
        details={"content_type": "text/plain", "allowed_content_types": ALLOWED},
    )
    assert len(repo) == 0


def test_ac3_image_jpg_alias_is_unsupported_media_type(client: TestClient) -> None:
    """Edge: image/jpg is not in the allow list (SPEC A2), so it is 415."""
    response = upload(client, "license.jpg", JPEG_BYTES, "image/jpg")

    assert response.status_code == 415
    assert_envelope(
        response.json(),
        code="unsupported_media_type",
        details={"content_type": "image/jpg", "allowed_content_types": ALLOWED},
    )


def test_ac3_bytes_not_matching_declared_png_returns_415_mismatch(
    client: TestClient, repo: repository.InMemoryDocumentRepository
) -> None:
    """Failure path: allowed type, wrong signature -> content_type_mismatch."""
    response = upload(client, "fake.png", b"not a png", "image/png")

    assert response.status_code == 415
    assert_envelope(
        response.json(), code="content_type_mismatch", details={"content_type": "image/png"}
    )
    assert len(repo) == 0


def test_ac3_pdf_bytes_declared_jpeg_returns_415_mismatch(client: TestClient) -> None:
    """Failure path: a real PDF mislabeled as JPEG is a mismatch, not a 201."""
    response = upload(client, "license.jpg", PDF_BYTES, "image/jpeg")

    assert response.status_code == 415
    assert_envelope(
        response.json(), code="content_type_mismatch", details={"content_type": "image/jpeg"}
    )


# --------------------------------------------------------------------------- AC4


def test_ac4_empty_file_returns_400_empty_file(
    client: TestClient, repo: repository.InMemoryDocumentRepository
) -> None:
    """Happy path: a zero-byte part declared application/pdf is empty_file."""
    response = upload(client, "empty.pdf", b"", "application/pdf")

    assert response.status_code == 400
    assert_envelope(response.json(), code="empty_file", details={})
    assert len(repo) == 0


def test_ac4_missing_file_part_returns_422_validation_error(client: TestClient) -> None:
    """Edge: a multipart body without a `file` part is the 422 envelope."""
    response = client.post("/documents", files={"nope": ("a.png", PNG_BYTES, "image/png")})

    assert response.status_code == 422
    body = response.json()
    assert_envelope(body, code="validation_error")
    errors = body["error"]["details"]["errors"]
    assert isinstance(errors, list)
    assert len(errors) > 0


def test_ac4_validate_upload_raises_api_error_for_empty_content() -> None:
    """Failure path: the validator itself raises ApiError(400, empty_file)."""
    with pytest.raises(ApiError) as excinfo:
        uploads.validate_upload(b"", "application/pdf", Settings())

    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "empty_file"
    assert excinfo.value.details == {}


# AC4 / SPEC A17: the 422 path must never echo the uploaded bytes back to the client.


def test_ac4_empty_filename_part_does_not_echo_payload_bytes(
    client: TestClient, repo: repository.InMemoryDocumentRepository
) -> None:
    """Failure path: a part with an empty filename is 422 and leaks none of its bytes.

    An empty filename makes the part arrive as a plain form field rather than an UploadFile,
    so request validation fails while holding the whole payload.
    """
    response = client.post("/documents", files={"file": ("", SECRET_PNG_BYTES, "image/png")})

    assert response.status_code == 422
    assert_envelope(response.json(), code="validation_error")
    assert_no_payload_leak(response, SECRET_MARKER)
    assert "PNG" not in response.text
    assert len(repo) == 0


def test_ac4_non_file_form_field_does_not_echo_payload_bytes(
    client: TestClient, repo: repository.InMemoryDocumentRepository
) -> None:
    """Edge: a plain (non-file) form field named `file` is 422 and leaks none of its content."""
    response = client.post("/documents", data={"file": f"{SECRET_MARKER}-as-a-form-field"})

    assert response.status_code == 422
    assert_envelope(response.json(), code="validation_error")
    assert_no_payload_leak(response, SECRET_MARKER)
    assert len(repo) == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"files": {"file": ("", SECRET_PNG_BYTES, "image/png")}},
        {"data": {"file": f"{SECRET_MARKER}-as-a-form-field"}},
        {"files": {"nope": ("a.png", PNG_BYTES, "image/png")}},
    ],
    ids=["empty-filename", "form-field", "missing-part"],
)
def test_ac4_validation_errors_keep_type_loc_msg_and_drop_input_and_ctx(
    client: TestClient, kwargs: dict[str, Any]
) -> None:
    """Happy path for the contract: details.errors stays useful but carries no echoed input."""
    response = client.post("/documents", **kwargs)

    assert response.status_code == 422
    body = response.json()
    assert_envelope(body, code="validation_error")
    errors = body["error"]["details"]["errors"]
    assert isinstance(errors, list)
    assert len(errors) > 0
    for entry in errors:
        assert isinstance(entry, dict)
        assert {"type", "loc", "msg"} <= set(entry.keys())
        assert isinstance(entry["type"], str)
        assert isinstance(entry["loc"], list)
        assert isinstance(entry["msg"], str)
        assert "input" not in entry
        assert "ctx" not in entry


# --------------------------------------------------------------------------- AC5


def test_ac5_config_reports_default_limits(client: TestClient) -> None:
    """Happy path: GET /config states the 10 MB limit and the accepted types."""
    response = client.get("/config")

    assert response.status_code == 200
    assert response.json() == {
        "max_upload_bytes": 10485760,
        "allowed_content_types": ALLOWED,
    }


def test_ac5_config_reflects_custom_settings() -> None:
    """Edge: an injected limit is what the client is told."""
    response = client_with(Settings(max_upload_bytes=1024)).get("/config")

    assert response.status_code == 200
    assert response.json()["max_upload_bytes"] == 1024
    assert response.json()["allowed_content_types"] == ALLOWED


def test_ac5_post_config_returns_405_envelope(client: TestClient) -> None:
    """Failure path: /config is read-only; POST is the 405 envelope."""
    response = client.post("/config")

    assert response.status_code == 405
    assert_envelope(response.json(), code="method_not_allowed", details={})


# --------------------------------------------------------------------------- AC6


def test_ac6_unknown_document_id_returns_404_document_not_found(client: TestClient) -> None:
    """Happy path: an id that was never stored is document_not_found."""
    response = client.get("/documents/does-not-exist")

    assert response.status_code == 404
    assert_envelope(
        response.json(),
        code="document_not_found",
        details={"document_id": "does-not-exist"},
    )


def test_ac6_blank_document_id_returns_404_document_not_found(client: TestClient) -> None:
    """Edge: a whitespace-only id is looked up like any other id and is not found.

    (`GET /documents/` itself is consumed by Starlette's trailing-slash redirect, so the
    blank-id case is exercised with an id that survives routing.)
    """
    response = client.get("/documents/%20")

    assert response.status_code == 404
    assert_envelope(response.json(), code="document_not_found", details={"document_id": " "})


def test_ac6_repository_get_on_empty_repo_returns_none(
    repo: repository.InMemoryDocumentRepository,
) -> None:
    """Failure path: the repository reports a miss with None, it does not raise."""
    assert len(repo) == 0
    assert repo.get("x") is None


def test_ac6_neighbouring_id_is_not_found_after_a_successful_upload(
    client: TestClient, repo: repository.InMemoryDocumentRepository
) -> None:
    """Edge: a stored document does not make neighbouring ids resolve."""
    document_id = upload(client, "license.png", PNG_BYTES, "image/png").json()["id"]

    response = client.get(f"/documents/{document_id}x")

    assert response.status_code == 404
    assert_envelope(
        response.json(),
        code="document_not_found",
        details={"document_id": f"{document_id}x"},
    )
    assert repo.get(f"{document_id}x") is None
