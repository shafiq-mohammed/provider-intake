"""JSON error envelope and exception handlers (T-001)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

HTTP_STATUS_CODES: dict[int, str] = {
    404: "not_found",
    405: "method_not_allowed",
}


class ApiError(Exception):
    """An error that maps directly onto the JSON error envelope."""

    def __init__(
        self, status_code: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details: dict[str, Any] = details if details is not None else {}


def error_response(
    status_code: int, code: str, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    """Build the canonical error envelope response."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details if details else {},
            }
        },
    )


def safe_validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Project pydantic's validation errors down to the keys that are safe to return.

    ``exc.errors()`` carries the offending value under ``input`` (and sometimes ``ctx``).
    For an upload that is the document content itself, which must never reach the client
    or a log (SPEC A17). Only ``type``, ``loc`` and ``msg`` survive.
    """
    return [
        {key: jsonable_encoder(error[key]) for key in ("type", "loc", "msg") if key in error}
        for error in exc.errors()
    ]


def register_error_handlers(app: FastAPI) -> None:
    """Register the app-level handlers that produce the error envelope."""

    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            422,
            "validation_error",
            "Request validation failed.",
            {"errors": safe_validation_errors(exc)},
        )

    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = HTTP_STATUS_CODES.get(exc.status_code, "http_error")
        message = str(exc.detail) if exc.detail else "HTTP error."
        return error_response(exc.status_code, code, message)

    app.add_exception_handler(ApiError, handle_api_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, handle_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)  # type: ignore[arg-type]
