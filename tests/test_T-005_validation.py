"""Tests for T-005: deterministic normalization and validation rules.

Every date in this file is pinned. ``TODAY`` is a constant and app tests inject
``clock=lambda: TODAY`` through ``create_app`` (SPEC section 10), so the suite gives the same
answer on any calendar day. The single exception is the AC6 failure path, which deliberately
exercises the default clock with an expiry in 2099 and therefore stays ``found`` whenever it
runs.

The module-level ``app`` is never imported: it reads the process environment (SPEC A13).
The helpers below build clients, fakes and input bytes only. None of them normalizes a state,
parses a date, ranks a status or decides an issue code -- that is the behavior under test.
"""

from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import extraction, models, ocr, validation
from app.main import create_app

# --------------------------------------------------------------------------- constants

TODAY = date(2026, 9, 11)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_BYTES = PNG_SIGNATURE + b"\x00" * 32

OCR_TEXT = "License text"

# The six values of the AC5 cross product.
CROSS_PRODUCT_VALUES: tuple[str | None, ...] = (
    None,
    "",
    "Nonsense!!",
    "California",
    "01/31/2020",
    "2099-12-31",
)

# Every spelling of the same day that DATE_FORMATS must accept (AC4).
ACCEPTED_DATE_STRINGS: tuple[str, ...] = (
    "2027-06-30",
    "06/30/2027",
    "06-30-2027",
    "June 30, 2027",
    "Jun 30 2027",
    "30 June 2027",
)

EXPECTED_DATE_FORMATS: tuple[str, ...] = (
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

# --------------------------------------------------------------------------- input factories


def found(value: str | None, *, raw: str | None = None, issues: list[str] | None = None):
    """Factory only: a ``found`` FieldResult. Builds an input, decides nothing."""
    return models.FieldResult(
        value=value, status=models.FieldStatus.FOUND, raw=raw, issues=list(issues or [])
    )


def make_result(**overrides: Any) -> models.ExtractionResult:
    """Factory only: an all-``found`` ExtractionResult with the named fields replaced."""
    fields: dict[str, Any] = {
        "provider_name": found("Jane Doe"),
        "license_number": found("LCSW-12345"),
        "state": found("CA"),
        "expiration_date": found("2099-12-31"),
    }
    unknown = set(overrides) - set(fields)
    assert unknown == set(), f"make_result got unknown field names: {sorted(unknown)}"
    fields.update(overrides)
    return models.ExtractionResult(**fields)


def client_for(engine: Any, extractor: Any, clock: Any = None) -> TestClient:
    """Factory only: a client over an app with the collaborators injected.

    ``clock`` of ``None`` is passed through so the app falls back to its documented default.
    """
    return TestClient(create_app(ocr_engine=engine, field_extractor=extractor, clock=clock))


def upload_png(client: TestClient) -> str:
    """Factory only: upload one PNG through the real endpoint and return its id."""
    response = client.post("/documents", files={"file": ("license.png", PNG_BYTES, "image/png")})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def dump(field: Any) -> dict[str, Any]:
    """Factory only: the JSON shape of a FieldResult, for whole-object comparison."""
    return field.model_dump(mode="json")


# --------------------------------------------------------------------------- AC1


def test_ac1_full_state_name_normalizes_to_code() -> None:
    """Happy path: a full state name becomes the two-letter code, raw keeps what was read."""
    field = found("California", raw="California")

    result = validation.validate_field("state", field, today=TODAY)

    assert dump(result) == {
        "value": "CA",
        "status": "found",
        "raw": "California",
        "issues": [],
    }
    assert validation.normalize_state("California") == "CA"


def test_ac1_codes_case_and_trailing_punctuation_are_normalized() -> None:
    """Edge: canonical codes, lower case, surrounding space and trailing punctuation."""
    assert validation.normalize_state(" new york. ") == "NY"
    assert validation.normalize_state("tx") == "TX"
    assert validation.normalize_state("CA") == "CA"
    assert validation.normalize_state("District of Columbia") == "DC"
    assert validation.normalize_state("district of columbia") == "DC"
    assert validation.normalize_state("  california ;") == "CA"
    assert validation.normalize_state("Ca,") == "CA"

    # The documented code table: 50 states plus DC, keyed by lower-case full name (A11).
    assert isinstance(validation.US_STATE_CODES, dict)
    assert len(validation.US_STATE_CODES) == 51
    assert validation.US_STATE_CODES["district of columbia"] == "DC"
    assert validation.US_STATE_CODES["new york"] == "NY"
    assert all(key == key.lower() for key in validation.US_STATE_CODES)
    assert all(len(code) == 2 and code.isupper() for code in validation.US_STATE_CODES.values())

    # The same normalization observed through validate_field, raw copied from the value.
    result = validation.validate_field("state", found("tx"), today=TODAY)
    assert dump(result) == {"value": "TX", "status": "found", "raw": "tx", "issues": []}


def test_ac1_unknown_jurisdiction_is_invalid_with_unknown_state() -> None:
    """Failure path: a non-US jurisdiction is a downgrade to invalid, and "" is None."""
    field = found("Ontario")

    result = validation.validate_field("state", field, today=TODAY)

    assert result.status == models.FieldStatus.INVALID
    assert result.value is None
    assert result.issues == ["unknown_state"]
    assert result.raw == "Ontario"
    assert result.status.rank <= field.status.rank

    assert validation.normalize_state("") is None
    assert validation.normalize_state("   ") is None
    assert validation.normalize_state("Ontario") is None
    # Territories are unknown_state too (SPEC A11).
    assert validation.normalize_state("Puerto Rico") is None
    assert validation.normalize_state("PR") is None


# --------------------------------------------------------------------------- AC2


def test_ac2_unparseable_date_is_invalid_and_raw_is_copied_from_value() -> None:
    """Happy path: an impossible date is invalid, and raw is filled in from the value."""
    field = found("Jan 32 2024")
    assert field.raw is None

    result = validation.validate_field("expiration_date", field, today=TODAY)

    assert dump(result) == {
        "value": None,
        "status": "invalid",
        "raw": "Jan 32 2024",
        "issues": ["unparseable_date"],
    }
    assert validation.normalize_date("12/31/99") is None


def test_ac2_extractor_supplied_raw_is_preserved_verbatim() -> None:
    """Edge: raw is only ever copied from value when raw was None; otherwise it survives."""
    field = found("Jan 32 2024", raw="Exp:  Jan 32 2024  (smudged)")

    result = validation.validate_field("expiration_date", field, today=TODAY)

    assert result.raw == "Exp:  Jan 32 2024  (smudged)"
    assert result.value is None
    assert result.status == models.FieldStatus.INVALID
    assert result.issues == ["unparseable_date"]

    # A found field that stays found keeps its supplied raw too.
    kept = validation.validate_field(
        "state", found("California", raw="State of California"), today=TODAY
    )
    assert kept.raw == "State of California"
    assert kept.value == "CA"


def test_ac2_unparseable_inputs_append_the_code_once_and_keep_existing_issues() -> None:
    """Failure path: two-digit years and prose never parse; issue codes do not duplicate."""
    assert validation.normalize_date("12/31/99") is None
    assert validation.normalize_date("soon") is None
    assert validation.normalize_date("") is None
    assert validation.normalize_date("   ") is None

    with_existing = validation.validate_field(
        "expiration_date", found("soon", issues=["faint"]), today=TODAY
    )
    assert with_existing.issues == ["faint", "unparseable_date"]
    assert with_existing.status == models.FieldStatus.INVALID
    assert with_existing.value is None

    already_flagged = validation.validate_field(
        "expiration_date",
        found("soon", issues=["faint", "unparseable_date"]),
        today=TODAY,
    )
    assert already_flagged.issues == ["faint", "unparseable_date"]
    assert already_flagged.issues.count("unparseable_date") == 1


# --------------------------------------------------------------------------- AC3


def test_ac3_expired_date_is_invalid_with_null_value_and_visible_raw() -> None:
    """Happy path: a licence that expired is invalid, nulled, but still readable in raw."""
    field = found("01/31/2020", raw="01/31/2020")

    result = validation.validate_field("expiration_date", field, today=TODAY)

    assert dump(result) == {
        "value": None,
        "status": "invalid",
        "raw": "01/31/2020",
        "issues": ["expired"],
    }
    assert result.status.rank <= field.status.rank


def test_ac3_today_is_not_expired_but_yesterday_is() -> None:
    """Edge: the boundary. today is still valid, today minus one day is expired (A9)."""
    yesterday = TODAY - timedelta(days=1)
    assert TODAY.isoformat() == "2026-09-11"
    assert yesterday.isoformat() == "2026-09-10"

    on_today = validation.validate_field("expiration_date", found("2026-09-11"), today=TODAY)
    assert on_today.status == models.FieldStatus.FOUND
    assert on_today.value == "2026-09-11"
    assert on_today.issues == []

    day_before = validation.validate_field("expiration_date", found("2026-09-10"), today=TODAY)
    assert day_before.status == models.FieldStatus.INVALID
    assert day_before.value is None
    assert day_before.issues == ["expired"]
    assert day_before.raw == "2026-09-10"


def test_ac3_expired_date_keeps_supplied_raw_and_appends_expired_once() -> None:
    """Failure path: the original string survives in raw and expired is never duplicated."""
    field = found("01/31/2020", raw="Expires 01/31/2020", issues=["faint"])

    result = validation.validate_field("expiration_date", field, today=TODAY)

    assert result.value is None
    assert result.raw == "Expires 01/31/2020"
    assert result.issues == ["faint", "expired"]
    assert result.status == models.FieldStatus.INVALID

    already_flagged = validation.validate_field(
        "expiration_date", found("01/31/2020", issues=["expired"]), today=TODAY
    )
    assert already_flagged.issues == ["expired"]
    assert already_flagged.issues.count("expired") == 1
    assert already_flagged.raw == "01/31/2020"


# --------------------------------------------------------------------------- AC4


@pytest.mark.parametrize("raw_date", ACCEPTED_DATE_STRINGS)
def test_ac4_every_accepted_format_normalizes_to_iso(raw_date: str) -> None:
    """Happy path: all six spellings of 30 June 2027 land on the same ISO value."""
    result = validation.validate_field("expiration_date", found(raw_date), today=TODAY)

    assert result.status == models.FieldStatus.FOUND
    assert result.value == "2027-06-30"
    assert result.issues == []
    assert result.raw == raw_date
    assert validation.normalize_date(raw_date) == date(2027, 6, 30)


def test_ac4_internal_whitespace_is_collapsed_before_parsing() -> None:
    """Edge: surrounding and repeated internal whitespace do not defeat a known format."""
    assert validation.normalize_date("  June   30,  2027 ") == date(2027, 6, 30)
    assert validation.normalize_date("\t2027-06-30\n") == date(2027, 6, 30)

    result = validation.validate_field("expiration_date", found("  June   30,  2027 "), today=TODAY)
    assert result.status == models.FieldStatus.FOUND
    assert result.value == "2027-06-30"
    assert result.raw == "  June   30,  2027 "

    assert validation.DATE_FORMATS == EXPECTED_DATE_FORMATS


def test_ac4_impossible_and_non_us_orders_are_unparseable() -> None:
    """Failure path: month 13 never parses, and day-first numeric order is not accepted."""
    result = validation.validate_field("expiration_date", found("2027-13-01"), today=TODAY)

    assert result.status == models.FieldStatus.INVALID
    assert result.value is None
    assert result.issues == ["unparseable_date"]
    assert result.raw == "2027-13-01"

    assert validation.normalize_date("2027-13-01") is None
    # Numeric dates are US order, so a day-first 30/06 has no month 30 (SPEC A10).
    assert validation.normalize_date("30/06/2027") is None


# --------------------------------------------------------------------------- AC5


@pytest.mark.parametrize("value", CROSS_PRODUCT_VALUES)
@pytest.mark.parametrize("name", models.FIELD_NAMES)
@pytest.mark.parametrize("status", list(models.FieldStatus))
def test_ac5_validate_field_only_ever_downgrades(
    status: models.FieldStatus, name: str, value: str | None
) -> None:
    """The downgrade-only invariant over the full cross product (SPEC A8/A9, D2).

    ``original`` is a snapshot of the *constructed input object*, so the equality check below
    compares against what the model actually holds (FieldResult already nulls ``value`` for a
    non-found status) rather than against a value this test hopes survived.
    """
    field = models.FieldResult(status=status, value=value)
    original = field.model_copy(deep=True)

    result = validation.validate_field(name, field, today=TODAY)

    assert result is not field
    assert result.status.rank <= original.status.rank
    if original.status is models.FieldStatus.FOUND:
        assert result.status in (models.FieldStatus.FOUND, models.FieldStatus.INVALID)
        if result.status is models.FieldStatus.INVALID:
            assert result.value is None
            assert result.issues != []
    else:
        assert result == original
        assert result.status is original.status
        assert result.status is not models.FieldStatus.FOUND
        assert result.value is None
        assert result.issues == original.issues
        assert result.raw == original.raw
    # The input is never mutated in place.
    assert field == original


def test_ac5_found_but_blank_value_is_invalid_with_empty_value() -> None:
    """Edge: a found field with nothing in it is a downgrade, not a found empty string."""
    for blank in (None, "", "   ", "\n\t"):
        field = found(blank)
        result = validation.validate_field("provider_name", field, today=TODAY)

        assert result.status == models.FieldStatus.INVALID, blank
        assert result.value is None, blank
        assert result.issues == ["empty_value"], blank
        assert result.status.rank <= field.status.rank

    # Every field name treats a blank the same way.
    for name in models.FIELD_NAMES:
        blank_result = validation.validate_field(name, found(""), today=TODAY)
        assert blank_result.status == models.FieldStatus.INVALID, name
        assert blank_result.issues == ["empty_value"], name

    # Control: a non-blank name is stripped and stays found (SPEC A12).
    stripped = validation.validate_field("provider_name", found("  Jane Doe  "), today=TODAY)
    assert stripped.status == models.FieldStatus.FOUND
    assert stripped.value == "Jane Doe"
    assert stripped.raw == "  Jane Doe  "
    assert stripped.issues == []


def test_ac5_validate_field_returns_a_new_object() -> None:
    """Failure path: validation never hands back or edits the caller's FieldResult."""
    unchanged_input = models.FieldResult(status=models.FieldStatus.UNREADABLE, issues=["smudged"])
    unchanged = validation.validate_field("state", unchanged_input, today=TODAY)

    assert unchanged is not unchanged_input
    assert unchanged == unchanged_input
    # State is not shared: editing the answer cannot reach back into the extractor's result.
    unchanged.issues.append("mutated-by-the-caller")
    assert unchanged_input.issues == ["smudged"]

    downgraded_input = found("Ontario")
    downgraded = validation.validate_field("state", downgraded_input, today=TODAY)

    assert downgraded is not downgraded_input
    assert downgraded_input.status == models.FieldStatus.FOUND
    assert downgraded_input.value == "Ontario"
    assert downgraded_input.issues == []

    normalized_input = found("California")
    normalized = validation.validate_field("state", normalized_input, today=TODAY)

    assert normalized is not normalized_input
    assert normalized_input.value == "California"


def test_ac5_validate_result_leaves_an_all_unreadable_result_equal() -> None:
    """A result with nothing found passes through validation untouched (SPEC A8)."""
    original = models.ExtractionResult.all_with(models.FieldStatus.UNREADABLE)
    snapshot = original.model_copy(deep=True)

    result = validation.validate_result(original, today=TODAY)

    assert result == snapshot
    assert result is not original
    assert result.missing_fields == list(models.FIELD_NAMES)
    for name in models.FIELD_NAMES:
        field = getattr(result, name)
        assert field.status == models.FieldStatus.UNREADABLE, name
        assert field.value is None, name
    assert original == snapshot

    # A mixed result: only the found field is touched, the others come back equal.
    mixed = models.ExtractionResult(
        provider_name=found(" Jane Doe "),
        license_number=models.FieldResult(status=models.FieldStatus.NOT_FOUND),
        state=models.FieldResult(status=models.FieldStatus.UNREADABLE, raw="Calif?rnia"),
        expiration_date=models.FieldResult(status=models.FieldStatus.INVALID, issues=["torn"]),
    )
    validated = validation.validate_result(mixed, today=TODAY)

    assert validated.provider_name.value == "Jane Doe"
    assert validated.license_number == mixed.license_number
    assert validated.state == mixed.state
    assert validated.expiration_date == mixed.expiration_date
    assert validated.missing_fields == ["license_number", "state", "expiration_date"]


# --------------------------------------------------------------------------- AC6


def test_ac6_extract_endpoint_normalizes_and_flags_an_expired_licence() -> None:
    """Happy path: end to end with the injected clock, a full state name and a dead date."""
    engine = ocr.FakeOcrEngine(OCR_TEXT)
    extractor = extraction.FakeFieldExtractor(
        make_result(
            provider_name=found(" Jane Doe "),
            license_number=found("LCSW-12345"),
            state=found("California"),
            expiration_date=found("01/31/2020"),
        )
    )
    client = client_for(engine, extractor, clock=lambda: TODAY)
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document_id"] == document_id
    assert body["text_source"] == "ocr"
    assert body["provider_name"] == {
        "value": "Jane Doe",
        "status": "found",
        "raw": " Jane Doe ",
        "issues": [],
    }
    assert body["license_number"] == {
        "value": "LCSW-12345",
        "status": "found",
        "raw": "LCSW-12345",
        "issues": [],
    }
    assert body["state"] == {
        "value": "CA",
        "status": "found",
        "raw": "California",
        "issues": [],
    }
    assert body["expiration_date"] == {
        "value": None,
        "status": "invalid",
        "raw": "01/31/2020",
        "issues": ["expired"],
    }
    assert body["missing_fields"] == ["expiration_date"]
    assert extractor.calls == [OCR_TEXT]
    assert engine.calls == ["image/png"]


def test_ac6_no_text_short_circuit_is_untouched_by_validation() -> None:
    """Edge: the no_text result has nothing found, so validation is a no-op over it."""
    engine = ocr.FakeOcrEngine("   \n")
    extractor = extraction.FakeFieldExtractor(make_result(state=found("California")))
    client = client_for(engine, extractor, clock=lambda: TODAY)
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 200, response.text
    body = response.json()
    for name in models.FIELD_NAMES:
        assert body[name] == {
            "value": None,
            "status": "not_found",
            "raw": None,
            "issues": ["no_text"],
        }, name
    assert body["missing_fields"] == list(models.FIELD_NAMES)
    assert extractor.calls == []
    assert "CA" not in response.text


def test_ac6_default_clock_keeps_a_far_future_expiry_found() -> None:
    """Failure path: no clock injected. 2099-12-31 is in the future on any day this runs."""
    engine = ocr.FakeOcrEngine(OCR_TEXT)
    extractor = extraction.FakeFieldExtractor(
        make_result(state=found("California"), expiration_date=found("December 31, 2099"))
    )
    client = client_for(engine, extractor)
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expiration_date"] == {
        "value": "2099-12-31",
        "status": "found",
        "raw": "December 31, 2099",
        "issues": [],
    }
    assert body["state"]["value"] == "CA"
    assert body["missing_fields"] == []

    # The clock is a collaborator on app.state like every other one (SPEC section 5).
    def injected() -> date:
        return TODAY

    assert create_app(clock=injected).state.clock is injected
    default_clock = create_app().state.clock
    assert callable(default_clock)
    assert isinstance(default_clock(), date)
