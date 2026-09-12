"""Pydantic models for request/response shapes (T-002, T-003, T-004)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

FIELD_NAMES: tuple[str, ...] = ("provider_name", "license_number", "state", "expiration_date")


class DocumentMeta(BaseModel):
    """Public metadata for a stored document; never carries the bytes."""

    id: str
    filename: str
    content_type: str
    size_bytes: int


class TextExtraction(BaseModel):
    """The raw text of a document and where it came from."""

    text: str
    source: Literal["pdf_text_layer", "ocr"]
    char_count: int


class FieldStatus(StrEnum):
    """How certain the pipeline is about one extracted field."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    UNREADABLE = "unreadable"
    INVALID = "invalid"

    @property
    def rank(self) -> int:
        """Certainty rank: found=3 > invalid=2 > unreadable=1 > not_found=0 (SPEC A8)."""
        return _STATUS_RANKS[self]


_STATUS_RANKS: dict[FieldStatus, int] = {
    FieldStatus.FOUND: 3,
    FieldStatus.INVALID: 2,
    FieldStatus.UNREADABLE: 1,
    FieldStatus.NOT_FOUND: 0,
}


class FieldResult(BaseModel):
    """One extracted field: its value, how certain that value is, and why.

    Frozen: the A8 invariant below is enforced at construction, so no later assignment may
    put a value back on a non-``found`` field. Validation builds new instances (T-005).

    Two pydantic escape hatches skip validation by design and therefore skip the invariant:
    ``model_copy(update=...)`` and ``model_construct()``. Neither is used anywhere in
    ``src/``, so the invariant holds on every path that can reach a response. Build a new
    ``FieldResult`` instead of reaching for either.
    """

    model_config = ConfigDict(frozen=True)

    value: str | None = None
    status: FieldStatus
    raw: str | None = None
    issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _null_value_unless_found(self) -> FieldResult:
        """Force ``value`` to ``None`` whenever the status is not ``found`` (SPEC A8).

        A value attached to an uncertain field is dropped, never rejected, so a
        misbehaving extractor cannot put it on the wire. The model is frozen, so this last
        write during construction goes through ``object.__setattr__``; every later write
        is refused.
        """
        if self.status is not FieldStatus.FOUND and self.value is not None:
            object.__setattr__(self, "value", None)
        return self


class ExtractionResult(BaseModel):
    """The four credential fields, each with its own status."""

    provider_name: FieldResult
    license_number: FieldResult
    state: FieldResult
    expiration_date: FieldResult

    @computed_field  # type: ignore[prop-decorator]
    @property
    def missing_fields(self) -> list[str]:
        """Names in ``FIELD_NAMES`` order whose status is not ``found``."""
        return [
            name for name, field in self.fields().items() if field.status is not FieldStatus.FOUND
        ]

    def fields(self) -> dict[str, FieldResult]:
        """Return ``{name: FieldResult}`` in ``FIELD_NAMES`` order."""
        return {name: getattr(self, name) for name in FIELD_NAMES}

    @classmethod
    def all_with(cls, status: FieldStatus, *, issues: list[str] | None = None) -> ExtractionResult:
        """Build a result whose every field carries ``status`` and a copy of ``issues``."""
        return ExtractionResult(
            **{name: FieldResult(status=status, issues=list(issues or [])) for name in FIELD_NAMES}
        )


class ExtractionResponse(ExtractionResult):
    """An extraction result as returned by the API, tagged with its document and source."""

    document_id: str
    text_source: Literal["pdf_text_layer", "ocr"]
