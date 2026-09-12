"""Deterministic normalization and validation of extracted fields (T-005).

Everything here runs after the untrusted extractor has spoken. It only ever downgrades a
field's certainty: a ``found`` field becomes ``found`` with a normalized value or ``invalid``
with an issue code, and a field that was not ``found`` is returned unchanged (SPEC A8/A9).
"""

from __future__ import annotations

from datetime import date, datetime

from app.models import FIELD_NAMES, ExtractionResult, FieldResult, FieldStatus

US_STATE_CODES: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}
"""Lower-case full state name -> two-letter code: the 50 states plus DC.

Territories (PR, GU, VI, AS, MP) are deliberately absent; they are ``unknown_state`` (A11).
"""

_STATE_CODE_SET: frozenset[str] = frozenset(US_STATE_CODES.values())

DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
)
"""The only accepted date spellings, tried in order. Nothing else parses (A10)."""

_TRAILING_PUNCTUATION = ".,;"

# Issue codes set by this module.
EMPTY_VALUE = "empty_value"
UNKNOWN_STATE = "unknown_state"
UNPARSEABLE_DATE = "unparseable_date"
EXPIRED = "expired"

_PASS_THROUGH_FIELDS = ("provider_name", "license_number")


def normalize_state(raw: str) -> str | None:
    """Return the two-letter code for ``raw``, or ``None`` if it is not a US state or DC.

    Whitespace and trailing ``.,;`` are stripped and matching is case-insensitive. A
    two-letter code already in the code set is upper-cased; a full name is looked up in
    ``US_STATE_CODES``.
    """
    cleaned = raw.strip().rstrip(_TRAILING_PUNCTUATION).strip()
    if cleaned == "":
        return None
    if len(cleaned) == 2 and cleaned.upper() in _STATE_CODE_SET:
        return cleaned.upper()
    return US_STATE_CODES.get(cleaned.lower())


def normalize_date(raw: str) -> date | None:
    """Parse ``raw`` with ``DATE_FORMATS`` in order and return the first hit, else ``None``.

    Surrounding whitespace is stripped and internal whitespace runs collapse to one space.
    """
    cleaned = " ".join(raw.split())
    if cleaned == "":
        return None
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, date_format).date()
        except ValueError:
            continue
    return None


def validate_field(name: str, field: FieldResult, *, today: date) -> FieldResult:
    """Return a NEW ``FieldResult`` for ``name``, never mutating ``field``.

    A field whose status is not ``found`` comes back equal to its input. A ``found`` field is
    normalized per ``name`` and stays ``found``, or is downgraded to ``invalid`` with an issue
    code appended. ``result.status.rank <= field.status.rank`` always holds (A8).
    """
    if field.status is not FieldStatus.FOUND:
        return field.model_copy(deep=True)

    raw = field.raw if field.raw is not None else field.value
    value = field.value

    if value is None or value.strip() == "":
        return _invalid(field, raw, EMPTY_VALUE)

    if name == "state":
        code = normalize_state(value)
        if code is None:
            return _invalid(field, raw, UNKNOWN_STATE)
        return _found(field, raw, code)

    if name == "expiration_date":
        parsed = normalize_date(value)
        if parsed is None:
            return _invalid(field, raw, UNPARSEABLE_DATE)
        if parsed < today:
            # The date stays readable in ``raw``; ``value`` is nulled like any non-found field.
            return _invalid(field, raw, EXPIRED)
        return _found(field, raw, parsed.isoformat())

    # provider_name, license_number and anything else: whitespace stripping only (A12).
    return _found(field, raw, value.strip())


def validate_result(result: ExtractionResult, *, today: date) -> ExtractionResult:
    """Return a new ``ExtractionResult`` with every field validated (``missing_fields`` follows)."""
    return ExtractionResult(
        **{name: validate_field(name, getattr(result, name), today=today) for name in FIELD_NAMES}
    )


def _found(field: FieldResult, raw: str | None, value: str) -> FieldResult:
    """A ``found`` field with a normalized value and the incoming issues preserved."""
    return FieldResult(value=value, status=FieldStatus.FOUND, raw=raw, issues=list(field.issues))


def _invalid(field: FieldResult, raw: str | None, code: str) -> FieldResult:
    """An ``invalid`` field with a null value and ``code`` appended once to the issues."""
    return FieldResult(
        value=None, status=FieldStatus.INVALID, raw=raw, issues=_with_issue(field.issues, code)
    )


def _with_issue(issues: list[str], code: str) -> list[str]:
    """A copy of ``issues`` with ``code`` appended unless it is already present."""
    if code in issues:
        return list(issues)
    return [*issues, code]
