"""Application settings (T-001)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

ENV_FIELDS: dict[str, str] = {
    "APP_MAX_UPLOAD_BYTES": "max_upload_bytes",
    "APP_MIN_PDF_TEXT_CHARS": "min_pdf_text_chars",
    "APP_OCR_BACKEND": "ocr_backend",
    "APP_EXTRACTOR_BACKEND": "extractor_backend",
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "APP_ANTHROPIC_MODEL": "anthropic_model",
}


class Settings(BaseModel):
    """Immutable application configuration."""

    model_config = ConfigDict(frozen=True)

    max_upload_bytes: int = 10 * 1024 * 1024
    allowed_content_types: tuple[str, ...] = ("application/pdf", "image/jpeg", "image/png")
    min_pdf_text_chars: int = 20
    ocr_backend: Literal["fake", "anthropic"] = "fake"
    extractor_backend: Literal["fake", "anthropic"] = "fake"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from a mapping, defaulting to ``os.environ``.

        Unset keys keep their defaults; ``allowed_content_types`` is not configurable
        from the environment. Invalid values raise ``pydantic.ValidationError``.
        """
        source: Mapping[str, str] = os.environ if env is None else env
        values: dict[str, Any] = {}
        for env_key, field_name in ENV_FIELDS.items():
            if env_key in source:
                values[field_name] = source[env_key]
        return cls(**values)
