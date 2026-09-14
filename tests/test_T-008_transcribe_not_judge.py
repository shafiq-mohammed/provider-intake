"""Tests for T-008: the extractor transcribes; judgments become advisory issues.

Two things this file deliberately does *not* claim:

1. **AC1 asserts on prompt text, which cannot prove the live model obeys the prompt.** The
   assertions below check that ``EXTRACTION_PROMPT`` carries the intent required by the ticket
   (transcribe, record concerns as issue codes, keep ``found``, reserve ``invalid`` for
   deterministic validation) and that it does not tell the model to mark a document it distrusts
   ``invalid``. Whether Claude actually complies is observable only in a live run with a real
   API key; no stub can establish it. Treat AC1 as a guard on the instruction we send, not as
   evidence about model behaviour.
2. The keyword checks are intentionally loose -- keyword/intent, never exact sentences -- so the
   prompt may be reworded freely. Where a sentence-level check is used (the ``invalid`` one) the
   prompt is split into sentence-ish segments and every segment naming ``invalid`` must also
   carry a reservation marker; any phrasing that reserves ``invalid`` for our validator passes.

Everything here runs against a stub client: an object exposing ``messages.create(**kwargs)`` that
records the kwargs and replays a canned reply. The stub is an *input*; it parses nothing, decides
nothing and never reaches the network (several tests also install the ``no_network`` guard). No
API key is read anywhere in this file. ``TODAY`` is fixed at 2026-09-14, so "expired" is a
property of the fixture, not of the calendar.

AC3/AC4/AC5 pin behaviour of ``validate_result`` that T-008 must *not* change: advisory issue
codes survive validation untouched, deterministic codes are appended rather than substituted, and
``not_found`` / ``unreadable`` still mean the value is absent. They are regression guards.
"""

import json
import re
import socket
import types
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import models, ocr, validation
from app.main import create_app

# --------------------------------------------------------------------------- constants

MODEL = "model-x"
TODAY = date(2026, 9, 14)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

OCR_TEXT = "DIAGNOSTIC MEDICINE -- LICENSE#: SARCASM"

# The ticket's novelty licence: a legible value the model distrusts.
SARCASM_FIELD = {
    "value": "SARCASM",
    "status": "found",
    "raw": "SARCASM",
    "issues": ["appears_fictional"],
}
SARCASM_JSON = json.dumps({"license_number": SARCASM_FIELD})

# AC4's combination: one advisory-only field and one advisory field that is also expired.
ADVISORY_AND_EXPIRED = {
    "provider_name": {
        "value": "Gregory House",
        "status": "found",
        "raw": "Gregory House, M.D.",
        "issues": ["low_contrast"],
    },
    "license_number": SARCASM_FIELD,
    "state": {"value": "NJ", "status": "found", "raw": "Princeton, NJ 08540", "issues": []},
    "expiration_date": {
        "value": "06-11-2015",
        "status": "found",
        "raw": "06-11-2015",
        "issues": ["appears_fictional"],
    },
}
ADVISORY_AND_EXPIRED_JSON = json.dumps(ADVISORY_AND_EXPIRED)

# --------------------------------------------------------------------------- AC1 keyword sets
# Intent, not wording: each group lists synonyms any reasonable phrasing would hit.

TRANSCRIBE_WORDS = ("transcrib", "verbatim", "as written", "copy", "record what")
JUDGMENT_WORDS = (
    "authentic",
    "genuine",
    "fictional",
    "forg",
    "judge",
    "judging",
    "judgment",
    "judgement",
    "validity",
    "legitima",
    "plausib",
)
CONCERN_WORDS = ("concern", "doubt", "suspici", "observ", "record", "note", "flag", "report")
KEEP_WORDS = ("keep", "keeping", "still", "remain", "leave", "leaving", "stay")
RESERVATION_WORDS = (
    "deterministic",
    "reserved",
    "reserve",
    "never",
    "do not",
    "don't",
    "not yours",
    "downstream",
    "our validation",
    "only set by",
)
VALIDATOR_WORDS = ("deterministic", "validation", "validator", "validate")

# --------------------------------------------------------------------------- input factories


class StubClient:
    """Input factory only: records every ``messages.create`` kwargs and replays a canned reply.

    It does not parse JSON, validate anything or decide a status; that is the code under test.
    """

    def __init__(self, *texts: str, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._error = error
        self._blocks = [types.SimpleNamespace(type="text", text=text) for text in texts]
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return types.SimpleNamespace(content=list(self._blocks))


def adapters() -> Any:
    """Import the module under test at call time, matching the T-007 suite's style."""
    from app import anthropic_adapters

    return anthropic_adapters


def extract(payload: dict[str, Any]) -> models.ExtractionResult:
    """Drive the real extractor with a stub replaying ``payload`` as the model's JSON reply."""
    stub = StubClient(json.dumps(payload))
    result = adapters().AnthropicFieldExtractor(stub, MODEL).extract_fields(OCR_TEXT)
    assert len(stub.calls) == 1
    return result


def validated(payload: dict[str, Any]) -> models.ExtractionResult:
    """Extract ``payload`` through the stub, then run the deterministic layer at ``TODAY``."""
    return validation.validate_result(extract(payload), today=TODAY)


def dump(field: Any) -> dict[str, Any]:
    """Read helper: the JSON shape of a FieldResult, for whole-object comparison."""
    return field.model_dump(mode="json")


def prompt_text() -> str:
    """Read helper: the extraction prompt, lower-cased."""
    return str(adapters().EXTRACTION_PROMPT).lower()


def prompt_segments() -> list[str]:
    """Read helper: the prompt split into sentence-ish, lower-cased segments."""
    return [seg.strip() for seg in re.split(r"(?<=[.;:])\s+", prompt_text()) if seg.strip()]


def mentions(text: str, words: tuple[str, ...]) -> bool:
    """True when ``text`` contains any of ``words``."""
    return any(word in text for word in words)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard, not a stub: opening a socket during the test is a failure, not a silent call."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("this test must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def upload_png(client: TestClient) -> str:
    """Factory only: upload one PNG through the real endpoint and return its id."""
    response = client.post("/documents", files={"file": ("license.png", PNG_BYTES, "image/png")})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


# --------------------------------------------------------------------------- AC1


def test_ac1_prompt_instructs_transcription_not_judgment() -> None:
    """Happy path: the prompt carries all four instructions the ticket requires, by intent."""
    prompt = prompt_text()
    segments = prompt_segments()

    assert isinstance(adapters().EXTRACTION_PROMPT, str) and prompt.strip() != ""
    assert mentions(prompt, TRANSCRIBE_WORDS), "the prompt never asks the model to transcribe"
    assert mentions(prompt, JUDGMENT_WORDS), (
        "the prompt never tells the model to leave authenticity / format validity alone"
    )
    assert "format" in prompt, "the prompt never mentions format judgments"
    assert "issue" in prompt and mentions(prompt, CONCERN_WORDS), (
        "the prompt never tells the model to record its concerns as issue codes"
    )
    assert "found" in prompt and mentions(prompt, KEEP_WORDS), (
        "the prompt never says the status stays found while a concern is recorded"
    )
    assert any("invalid" in seg and mentions(seg, VALIDATOR_WORDS) for seg in segments), (
        "no sentence reserves 'invalid' for deterministic validation"
    )


def test_ac1_prompt_still_defines_not_found_unreadable_and_the_output_shape() -> None:
    """Edge: the new instructions do not cost us the status vocabulary or the JSON contract."""
    prompt = prompt_text()

    assert "not_found" in prompt
    assert "unreadable" in prompt
    assert mentions(prompt, ("absent", "missing", "not present", "not in the")), (
        "the prompt no longer says not_found means the field is absent"
    )
    assert mentions(prompt, ("cannot read", "can't read", "illegible", "unable to read")), (
        "the prompt no longer says unreadable means the model cannot read the field"
    )
    assert "snake_case" in prompt
    assert "json" in prompt
    for name in models.FIELD_NAMES:
        assert name in prompt, name


def test_ac1_prompt_does_not_tell_the_model_to_mark_documents_invalid() -> None:
    """Failure path: no sentence offers 'invalid' as a judgment the model may make.

    Every segment naming ``invalid`` must also carry a reservation marker ("never", "reserved",
    "deterministic", "do not", ...), so a sentence that merely forbids the status passes while
    the T-007 sentence -- which lists ``invalid`` among the statuses the model may choose --
    does not.
    """
    offenders = [
        seg
        for seg in prompt_segments()
        if "invalid" in seg and not mentions(seg, RESERVATION_WORDS)
    ]

    assert offenders == [], (
        f"the prompt still offers 'invalid' to the model without reserving it: {offenders}"
    )


# --------------------------------------------------------------------------- AC2


def test_ac2_novelty_licence_number_is_transcribed_and_stays_found() -> None:
    """Happy path: a distrusted but legible value stays a value, with the doubt beside it."""
    result = extract({"license_number": SARCASM_FIELD})

    assert dump(result.license_number) == SARCASM_FIELD
    assert result.license_number.value == "SARCASM"
    assert result.license_number.status == models.FieldStatus.FOUND
    assert result.license_number.issues == ["appears_fictional"]
    assert "license_number" not in result.missing_fields
    assert result.missing_fields == ["provider_name", "state", "expiration_date"]


def test_ac2_two_advisory_issues_on_one_field_both_survive() -> None:
    """Edge: several concerns on one field are all preserved, in order, and it is still found."""
    field = {
        "value": "SARCASM",
        "status": "found",
        "raw": "LICENSE#: SARCASM",
        "issues": ["appears_fictional", "not_a_standard_format"],
    }

    result = extract({"license_number": field})

    assert dump(result.license_number) == field
    assert result.license_number.issues == ["appears_fictional", "not_a_standard_format"]
    assert result.license_number.status == models.FieldStatus.FOUND
    assert result.license_number.value == "SARCASM"


def test_ac2_field_the_model_omits_is_still_not_found() -> None:
    """Failure path: transcribing more does not invent fields the reply never mentioned."""
    result = extract({"provider_name": {"value": "Gregory House", "status": "found"}})

    for name in ("license_number", "state", "expiration_date"):
        field = getattr(result, name)
        assert field.status == models.FieldStatus.NOT_FOUND, name
        assert field.value is None, name
        assert field.raw is None, name
        assert field.issues == [], name
        assert name in result.missing_fields, name
    assert result.provider_name.status == models.FieldStatus.FOUND


# --------------------------------------------------------------------------- AC3


def test_ac3_validation_returns_the_transcribed_field_unchanged() -> None:
    """Happy path: the deterministic layer neither rejects the value nor strips the issue code."""
    result = extract({"license_number": SARCASM_FIELD})

    checked = validation.validate_result(result, today=TODAY)

    assert dump(checked.license_number) == SARCASM_FIELD
    assert dump(checked.license_number) == dump(result.license_number)
    assert checked.license_number.value == "SARCASM"
    assert checked.license_number.status == models.FieldStatus.FOUND
    assert checked.license_number.issues == ["appears_fictional"]
    assert "license_number" not in checked.missing_fields


def test_ac3_advisory_issue_on_state_survives_normalization() -> None:
    """Edge: normalizing the state rewrites ``value`` only; the model's issue code rides along.

    The deterministic layer parses a state code or a full state name, never an address line, so
    the address stays in ``raw`` and the model supplies the normalized ``value`` -- the arrow
    "Princeton, NJ 08540" -> "NJ" is the model's transcription plus our normalization together.
    """
    checked = validated(
        {
            "state": {
                "value": "NJ",
                "status": "found",
                "raw": "Princeton, NJ 08540",
                "issues": ["inferred_from_address"],
            }
        }
    )

    assert dump(checked.state) == {
        "value": "NJ",
        "status": "found",
        "raw": "Princeton, NJ 08540",
        "issues": ["inferred_from_address"],
    }
    assert "state" not in checked.missing_fields

    # The same holds when the model transcribes the full name and we shorten it.
    spelled_out = validated(
        {
            "state": {
                "value": "New Jersey",
                "status": "found",
                "raw": "Princeton, New Jersey 08540",
                "issues": ["inferred_from_address"],
            }
        }
    )
    assert spelled_out.state.value == "NJ"
    assert spelled_out.state.status == models.FieldStatus.FOUND
    assert spelled_out.state.issues == ["inferred_from_address"]


def test_ac3_unknown_state_still_gets_the_deterministic_code_appended() -> None:
    """Failure path: a jurisdiction we cannot resolve is still invalid, advisory issue and all."""
    checked = validated(
        {
            "state": {
                "value": "Freedonia",
                "status": "found",
                "raw": "Freedonia",
                "issues": ["inferred_from_address"],
            }
        }
    )

    assert dump(checked.state) == {
        "value": None,
        "status": "invalid",
        "raw": "Freedonia",
        "issues": ["inferred_from_address", validation.UNKNOWN_STATE],
    }
    assert "state" in checked.missing_fields

    # An un-normalized address as the *value* is an unknown state too: our parser reads codes
    # and full names only. Pinned so the division of labour is explicit, not accidental.
    address = validated(
        {"state": {"value": "Princeton, NJ 08540", "status": "found", "raw": "Princeton, NJ 08540"}}
    )
    assert address.state.status == models.FieldStatus.INVALID
    assert address.state.issues == [validation.UNKNOWN_STATE]


# --------------------------------------------------------------------------- AC4


def test_ac4_advisory_issues_are_kept_and_deterministic_codes_appended() -> None:
    """Happy path: doubt alone never downgrades; an expired date still does, keeping the doubt."""
    checked = validated(ADVISORY_AND_EXPIRED)

    assert dump(checked.provider_name) == {
        "value": "Gregory House",
        "status": "found",
        "raw": "Gregory House, M.D.",
        "issues": ["low_contrast"],
    }
    assert dump(checked.expiration_date) == {
        "value": None,
        "status": "invalid",
        "raw": "06-11-2015",
        "issues": ["appears_fictional", validation.EXPIRED],
    }
    # The advisory code comes first and the deterministic one is appended, not substituted.
    assert checked.expiration_date.issues[0] == "appears_fictional"
    assert checked.expiration_date.issues[-1] == validation.EXPIRED
    assert dump(checked.license_number) == SARCASM_FIELD
    assert checked.missing_fields == ["expiration_date"]


def test_ac4_deterministic_code_is_not_duplicated_and_order_is_stable() -> None:
    """Edge: the model already said "expired"; we do not say it twice, and order is preserved."""
    checked = validated(
        {
            "expiration_date": {
                "value": "06-11-2015",
                "status": "found",
                "raw": "06-11-2015",
                "issues": ["appears_fictional", "expired", "low_contrast"],
            }
        }
    )

    issues = checked.expiration_date.issues
    assert issues == ["appears_fictional", "expired", "low_contrast"]
    assert issues.count(validation.EXPIRED) == 1
    assert len(issues) == len(set(issues))
    assert checked.expiration_date.status == models.FieldStatus.INVALID
    assert checked.expiration_date.value is None
    assert checked.expiration_date.raw == "06-11-2015"


def test_ac4_unparseable_and_empty_values_append_rather_than_replace() -> None:
    """Failure path: every deterministic code lands beside the advisory ones, never instead."""
    checked = validated(
        {
            "expiration_date": {
                "value": "the fourth of July",
                "status": "found",
                "raw": "Exp: the fourth of July",
                "issues": ["smudged"],
            },
            "provider_name": {
                "value": "   ",
                "status": "found",
                "raw": "   ",
                "issues": ["blank_field"],
            },
        }
    )

    assert dump(checked.expiration_date) == {
        "value": None,
        "status": "invalid",
        "raw": "Exp: the fourth of July",
        "issues": ["smudged", validation.UNPARSEABLE_DATE],
    }
    assert dump(checked.provider_name) == {
        "value": None,
        "status": "invalid",
        "raw": "   ",
        "issues": ["blank_field", validation.EMPTY_VALUE],
    }
    # Both downgraded fields are missing; the two the reply omitted are not_found and so are too.
    assert checked.missing_fields == list(models.FIELD_NAMES)


def test_ac4_endpoint_returns_the_transcribed_value_and_both_issue_kinds(
    no_network: None,
) -> None:
    """The same combination on the wire: what the UI's Issues column is handed.

    Uses the real /extract endpoint with a fixed clock, a fake OCR engine and the stub-backed
    real extractor -- no network, no key.
    """
    mod = adapters()
    stub = StubClient(ADVISORY_AND_EXPIRED_JSON)
    client = TestClient(
        create_app(
            ocr_engine=ocr.FakeOcrEngine(OCR_TEXT),
            field_extractor=mod.AnthropicFieldExtractor(stub, MODEL),
            clock=lambda: TODAY,
        )
    )
    document_id = upload_png(client)

    response = client.post(f"/documents/{document_id}/extract")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["license_number"] == SARCASM_FIELD
    assert body["provider_name"]["status"] == "found"
    assert body["provider_name"]["issues"] == ["low_contrast"]
    assert body["expiration_date"]["status"] == "invalid"
    assert body["expiration_date"]["value"] is None
    assert body["expiration_date"]["raw"] == "06-11-2015"
    assert body["expiration_date"]["issues"] == ["appears_fictional", "expired"]
    assert body["missing_fields"] == ["expiration_date"]


# --------------------------------------------------------------------------- AC5


def test_ac5_not_found_and_unreadable_pass_through_unchanged() -> None:
    """Happy path: transcribing more must not stop us reporting an absent or illegible field."""
    checked = validated(
        {
            "provider_name": {"value": "Gregory House", "status": "found", "raw": "Gregory House"},
            "license_number": SARCASM_FIELD,
            "state": {"value": None, "status": "not_found", "raw": None, "issues": []},
            "expiration_date": {
                "value": None,
                "status": "unreadable",
                "raw": "EXP: ██/██/██",
                "issues": ["glare"],
            },
        }
    )

    assert dump(checked.state) == {"value": None, "status": "not_found", "raw": None, "issues": []}
    assert dump(checked.expiration_date) == {
        "value": None,
        "status": "unreadable",
        "raw": "EXP: ██/██/██",
        "issues": ["glare"],
    }
    assert checked.missing_fields == ["state", "expiration_date"]
    assert checked.license_number.value == "SARCASM"


def test_ac5_value_on_a_not_found_field_is_still_nulled() -> None:
    """Edge: a value smuggled onto a non-found field never reaches a response (A8 holds)."""
    result = extract(
        {
            "state": {"value": "NJ", "status": "not_found", "raw": "NJ", "issues": []},
            "expiration_date": {
                "value": "2099-12-31",
                "status": "unreadable",
                "raw": "2099-12-31",
                "issues": ["glare"],
            },
        }
    )

    checked = validation.validate_result(result, today=TODAY)

    assert result.state.value is None
    assert checked.state.value is None
    assert checked.state.status == models.FieldStatus.NOT_FOUND
    assert checked.expiration_date.value is None
    assert checked.expiration_date.status == models.FieldStatus.UNREADABLE
    assert checked.expiration_date.issues == ["glare"]
    assert "state" in checked.missing_fields
    assert "expiration_date" in checked.missing_fields


def test_ac5_all_four_missing_stay_missing_and_are_never_promoted() -> None:
    """Failure path: an unreadable document reports four missing fields, promoted by nothing."""
    payload = {
        "provider_name": {"value": None, "status": "not_found", "issues": []},
        "license_number": {"value": None, "status": "unreadable", "issues": ["illegible"]},
        "state": {"value": None, "status": "not_found", "issues": []},
        "expiration_date": {"value": None, "status": "unreadable", "issues": ["illegible"]},
    }
    result = extract(payload)

    checked = validation.validate_result(result, today=TODAY)

    assert checked.missing_fields == list(models.FIELD_NAMES)
    for name in models.FIELD_NAMES:
        before = getattr(result, name)
        after = getattr(checked, name)
        assert after.value is None, name
        assert after.status == before.status, name
        assert after.status.rank <= before.status.rank, name
        assert after.issues == before.issues, name
