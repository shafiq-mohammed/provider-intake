"""The OCR engine protocol, its fake and the backend selector (T-003)."""

from __future__ import annotations

from typing import Protocol

from app.settings import Settings


class OcrError(Exception):
    """Raised by ``extract_text()`` when the engine fails; wraps the original as ``__cause__``."""


class OcrEngine(Protocol):
    """An engine that turns document bytes into raw text."""

    def extract_text(self, content: bytes, content_type: str) -> str:
        """Return the text of the document."""
        ...


class FakeOcrEngine:
    """A scripted engine for tests; records content types only, never the bytes."""

    def __init__(self, text: str = "", *, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[str] = []

    def extract_text(self, content: bytes, content_type: str) -> str:
        """Record the content type, raise the scripted error if set, else return the text."""
        self.calls.append(content_type)
        if self.error is not None:
            raise self.error
        return self.text


def build_ocr_engine(settings: Settings) -> OcrEngine:
    """Build the engine named by ``settings.ocr_backend``.

    ``"fake"`` builds a ``FakeOcrEngine``; ``"anthropic"`` builds an ``AnthropicOcrEngine`` on a
    real client, which is a ``ValueError`` without an API key (T-007). Every other backend is a
    ``ValueError``. The adapter module is imported inside the branch so the Anthropic SDK is not
    imported on the fake path.
    """
    if settings.ocr_backend == "fake":
        return FakeOcrEngine()
    if settings.ocr_backend == "anthropic":
        from app import anthropic_adapters

        return anthropic_adapters.AnthropicOcrEngine(
            anthropic_adapters.make_client(settings), settings.anthropic_model
        )
    raise ValueError(f"Unsupported ocr_backend: {settings.ocr_backend!r}")
