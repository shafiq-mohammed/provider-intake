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

    ``"fake"`` builds a ``FakeOcrEngine``; every other backend is a ``ValueError``
    until the real adapter arrives in T-007.
    """
    if settings.ocr_backend == "fake":
        return FakeOcrEngine()
    raise ValueError(f"Unsupported ocr_backend: {settings.ocr_backend!r}")
