"""The field extractor protocol, its fake and the backend selector (T-004)."""

from __future__ import annotations

from typing import Protocol

from app.models import ExtractionResult, FieldStatus
from app.settings import Settings


class ExtractorError(Exception):
    """Raised when the field extractor fails; wraps the original as ``__cause__``."""


class FieldExtractor(Protocol):
    """A backend that turns document text into the four credential fields."""

    def extract_fields(self, text: str) -> ExtractionResult:
        """Return the fields found in ``text``."""
        ...


class FakeFieldExtractor:
    """A scripted extractor for tests; records the text of each call, in order."""

    def __init__(
        self, result: ExtractionResult | None = None, *, error: Exception | None = None
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def extract_fields(self, text: str) -> ExtractionResult:
        """Record the text, raise the scripted error if set, else return the scripted result."""
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        if self.result is None:
            return ExtractionResult.all_with(FieldStatus.NOT_FOUND)
        return self.result


def build_field_extractor(settings: Settings) -> FieldExtractor:
    """Build the extractor named by ``settings.extractor_backend``.

    ``"fake"`` builds a ``FakeFieldExtractor``; every other backend is a ``ValueError``
    until the real adapter arrives in T-007.
    """
    if settings.extractor_backend == "fake":
        return FakeFieldExtractor()
    raise ValueError(f"Unsupported extractor_backend: {settings.extractor_backend!r}")
