"""Tests for T-001: app skeleton, settings, JSON error envelope, health check.

Every app under test is built with create_app(); the module-level `app` is never imported
because it reads the process environment (SPEC A13 / section 10).
"""

from typing import Any

import pytest
from app.errors import ApiError
from app.main import create_app
from app.settings import Settings
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError


def make_app() -> FastAPI:
    """Factory only: builds the app under test with hermetic defaults."""
    return create_app()


def make_client(app: FastAPI) -> TestClient:
    """Factory only: wraps an app in a TestClient."""
    return TestClient(app)


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


# --------------------------------------------------------------------------- AC1


def test_ac1_healthz_returns_200_ok_body() -> None:
    """Happy path: GET /healthz is 200 with body exactly {"status": "ok"}."""
    client = make_client(make_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ac1_healthz_content_type_is_json() -> None:
    """Edge: the health response is declared as JSON."""
    client = make_client(make_app())

    response = client.get("/healthz")

    assert response.headers["content-type"].startswith("application/json")


def test_ac1_app_state_settings_equals_default_settings() -> None:
    """create_app() with no settings stores Settings() defaults, not the environment."""
    app = make_app()

    assert app.state.settings == Settings()


def test_ac1_injected_settings_are_stored_on_app_state() -> None:
    """Edge: an explicitly passed Settings instance wins over the defaults."""
    injected = Settings(max_upload_bytes=4096)

    app = create_app(injected)

    assert app.state.settings == injected
    assert app.state.settings.max_upload_bytes == 4096


# --------------------------------------------------------------------------- AC2


def test_ac2_unknown_path_returns_404_envelope() -> None:
    """Happy path: an unknown route produces the not_found envelope."""
    client = make_client(make_app())

    response = client.get("/nope")

    assert response.status_code == 404
    assert_envelope(response.json(), code="not_found", details={})


def test_ac2_nested_unknown_path_returns_404_envelope() -> None:
    """Edge: a deeply nested unknown path behaves identically."""
    client = make_client(make_app())

    response = client.get("/a/b/c")

    assert response.status_code == 404
    assert_envelope(response.json(), code="not_found", details={})


def test_ac2_post_healthz_returns_405_method_not_allowed() -> None:
    """Failure path: a known path with the wrong method is 405 method_not_allowed."""
    client = make_client(make_app())

    response = client.post("/healthz")

    assert response.status_code == 405
    assert_envelope(response.json(), code="method_not_allowed", details={})


# --------------------------------------------------------------------------- AC3


def build_probe_app() -> FastAPI:
    """Registers a throwaway body-validating route on a fresh app (SPEC section 10)."""
    app = make_app()

    class Body(BaseModel):
        n: int

    @app.post("/_probe")
    def probe(body: Body) -> dict[str, int]:
        return {"n": body.n}

    return app


def test_ac3_non_integer_body_returns_422_validation_error() -> None:
    """Happy path: a non-integer value for a required int field is a 422 envelope."""
    client = make_client(build_probe_app())

    response = client.post("/_probe", json={"n": "x"})

    assert response.status_code == 422
    body = response.json()
    assert_envelope(body, code="validation_error")
    errors = body["error"]["details"]["errors"]
    assert isinstance(errors, list)
    assert len(errors) > 0


def test_ac3_empty_body_returns_422_validation_error() -> None:
    """Edge: no body at all is still the 422 envelope, never a 500."""
    client = make_client(build_probe_app())

    response = client.post("/_probe")

    assert response.status_code == 422
    body = response.json()
    assert_envelope(body, code="validation_error")
    assert len(body["error"]["details"]["errors"]) > 0


def test_ac3_valid_body_returns_200_proving_route_works() -> None:
    """Failure path for the handler: a valid body must reach the route untouched."""
    client = make_client(build_probe_app())

    response = client.post("/_probe", json={"n": 1})

    assert response.status_code == 200
    assert response.json() == {"n": 1}


# --------------------------------------------------------------------------- AC4


def build_api_error_app() -> FastAPI:
    """Registers throwaway routes that raise ApiError (SPEC section 10)."""
    app = make_app()

    @app.get("/_teapot")
    def teapot() -> None:
        raise ApiError(418, "teapot", "short and stout", {"size": "small"})

    @app.get("/_conflict")
    def conflict() -> None:
        raise ApiError(409, "conflict", "msg")

    def boom() -> None:
        raise ApiError(403, "forbidden", "nope", {"why": "dependency"})

    @app.get("/_dep", dependencies=[Depends(boom)])
    def dep() -> dict[str, str]:
        return {"unreachable": "yes"}

    return app


def test_ac4_api_error_maps_to_envelope() -> None:
    """Happy path: status, code, message and details come straight from the ApiError."""
    client = make_client(build_api_error_app())

    response = client.get("/_teapot")

    assert response.status_code == 418
    assert response.json() == {
        "error": {"code": "teapot", "message": "short and stout", "details": {"size": "small"}}
    }


def test_ac4_api_error_without_details_returns_empty_details() -> None:
    """Edge: omitted details serialize as {}, never null."""
    client = make_client(build_api_error_app())

    response = client.get("/_conflict")

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "conflict", "message": "msg", "details": {}}}


def test_ac4_api_error_raised_in_dependency_maps_to_envelope() -> None:
    """Failure path: an ApiError raised before the route body behaves identically."""
    client = make_client(build_api_error_app())

    response = client.get("/_dep")

    assert response.status_code == 403
    assert response.json() == {
        "error": {"code": "forbidden", "message": "nope", "details": {"why": "dependency"}}
    }


def test_ac4_api_error_exposes_attributes_and_defaults_details() -> None:
    """The exception object itself carries the documented attributes."""
    error = ApiError(409, "conflict", "msg")

    assert error.status_code == 409
    assert error.code == "conflict"
    assert error.message == "msg"
    assert error.details == {}


# --------------------------------------------------------------------------- AC5


def test_ac5_settings_defaults() -> None:
    """Happy path: defaults match the spec without touching the environment."""
    settings = Settings()

    assert settings.max_upload_bytes == 10 * 1024 * 1024
    assert settings.allowed_content_types == ("application/pdf", "image/jpeg", "image/png")
    assert settings.min_pdf_text_chars == 20
    assert settings.ocr_backend == "fake"
    assert settings.extractor_backend == "fake"
    assert settings.anthropic_api_key is None
    assert settings.anthropic_model == "claude-sonnet-5"


def test_ac5_from_env_maps_known_keys() -> None:
    """Happy path: an explicit mapping overrides only the documented keys."""
    settings = Settings.from_env(
        {
            "APP_MAX_UPLOAD_BYTES": "1024",
            "APP_OCR_BACKEND": "anthropic",
            "ANTHROPIC_API_KEY": "k",
        }
    )

    assert settings.max_upload_bytes == 1024
    assert settings.ocr_backend == "anthropic"
    assert settings.anthropic_api_key == "k"
    assert settings.extractor_backend == "fake"
    assert settings.anthropic_model == "claude-sonnet-5"
    assert settings.allowed_content_types == ("application/pdf", "image/jpeg", "image/png")


def test_ac5_from_env_empty_mapping_equals_defaults() -> None:
    """Edge: an empty mapping is exactly Settings()."""
    assert Settings.from_env({}) == Settings()


def test_ac5_from_env_invalid_int_raises_validation_error() -> None:
    """Failure path: a non-numeric int value is a pydantic ValidationError."""
    with pytest.raises(ValidationError):
        Settings.from_env({"APP_MAX_UPLOAD_BYTES": "abc"})


def test_ac5_from_env_invalid_backend_raises_validation_error() -> None:
    """Failure path: a backend outside the Literal is a pydantic ValidationError."""
    with pytest.raises(ValidationError):
        Settings.from_env({"APP_OCR_BACKEND": "magic"})


def test_ac5_settings_is_frozen() -> None:
    """Settings is immutable: assignment raises."""
    settings = Settings()

    with pytest.raises(ValidationError):
        settings.max_upload_bytes = 1
