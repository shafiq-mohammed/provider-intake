"""Tests for T-009: the results table falls back to ``raw`` when ``value`` is null.

Be blunt about what this file can and cannot prove. T-006 established the ceiling: **this suite
does not execute the page's JavaScript**. There is no browser, no jsdom, no JS engine here, only
the text of the HTML the server hands out. So AC1-AC4 assert the *source contract* -- that the
function which fills ``td[data-cell="value"]`` reads ``raw`` as a fallback for a null ``value``,
keeps the em-dash placeholder when there is no raw either, and carries the
``data-unconfirmed="true"`` marker on the fallback path. They cannot prove that a cell actually
renders ``06-11-2015`` on screen; nothing in CI clicks upload and reads the DOM. The test names
are deliberately about the *source*, not about what a reader sees. If the rendering itself must
be proven, that needs a jsdom-driven test (the ticket's own recommendation); until then the
rendering is verified by hand, not by this file.

AC5 is the one test here with real teeth: it drives ``POST /documents/{id}/extract`` with a
stubbed extractor and asserts the JSON is unchanged -- ``value`` still null, the field still in
``missing_fields``. That is the regression guard that stops a display change from quietly
becoming an API change. It is expected to be green before the page changes; it goes red if
someone "implements" T-009 by relaxing SPEC A8 or by editing ``validate_field`` instead of the
page.

Every date is pinned: apps are built with ``clock=lambda: TODAY`` so the suite answers the same
on any calendar day. Apps are built with ``create_app()``; the module-level ``app`` is never
imported (SPEC A13 / section 10). No network, no API key. The helpers below build clients,
inputs and fakes, and parse the served source. None of them renders a cell, chooses a
placeholder, or decides a status -- that is the behavior under test.
"""

import re
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import extraction, models, ocr
from app.main import create_app

# --------------------------------------------------------------------------- constants

TODAY = date(2026, 9, 14)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

OCR_TEXT = "License text"

EXPIRED_DATE_RAW = "06-11-2015"
UNKNOWN_STATE_RAW = "Ontario"

MARKER_ATTR = "data-unconfirmed"

# ``field.raw`` / ``field["raw"]`` -- a read of the raw property, however it is spelled.
RAW_RE = re.compile(r"\.raw\b|\[\s*[\"']raw[\"']\s*\]")
# ``field.value`` / ``field["value"]``. Deliberately does not match the cell selector
# ``cell(name, "value")``, which is a bare string argument.
VALUE_RE = re.compile(r"\.value\b|\[\s*[\"']value[\"']\s*\]")

# The marker, set as an attribute or through the dataset API.
MARKER_RE = re.compile(r"data-unconfirmed|dataset\s*\.\s*unconfirmed|dataset\s*\[")
# Any construct that takes the marker off again: removeAttribute, toggleAttribute, delete
# dataset.x, or assigning a falsy value.
# The assignment alternative is written so ``=== null`` (a comparison) cannot satisfy it.
MARKER_CLEAR_RE = re.compile(
    r"removeAttribute|toggleAttribute|delete\s+\w+\s*(\.|\[)"
    r"|(?<![=!<>])=\s*(null|false|\"\"|'')"
)
# Tokens that make a reference conditional rather than unconditional.
GUARD_RE = re.compile(r"null|undefined|\|\||\?\?|\?|!|if\b")

EM_DASH_SPELLINGS = ("—", "&mdash;", "\\u2014")

JS_BUILTINS = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "function",
        "return",
        "typeof",
        "String",
        "Number",
        "Boolean",
        "Array",
        "Object",
        "JSON",
        "fetch",
        "forEach",
        "join",
        "map",
        "filter",
        "then",
        "querySelector",
        "getElementById",
        "addEventListener",
        "setAttribute",
        "removeAttribute",
        "toggleAttribute",
        "preventDefault",
        "append",
        "encodeURIComponent",
        "toFixed",
        "isInteger",
        "json",
    }
)

# --------------------------------------------------------------------------- source parsing
# Readers of the served page, not writers of it. Nothing here knows what the fallback should
# say; it only locates the code that says it.


def script_source(page: str) -> str:
    """Every inline ``<script>`` body in the page, concatenated."""
    return "\n".join(re.findall(r"<script\b[^>]*>(.*?)</script>", page, re.IGNORECASE | re.DOTALL))


def style_source(page: str) -> str:
    """Every ``<style>`` body in the page, concatenated."""
    return "\n".join(re.findall(r"<style\b[^>]*>(.*?)</style>", page, re.IGNORECASE | re.DOTALL))


def function_body(source: str, name: str) -> str | None:
    """The brace-matched body of ``function name(...) { ... }``, or None if it is not there.

    Also accepts ``var name = function (...) {`` and ``const name = (...) =>  {``, so the coder
    keeps latitude over how the function is declared.
    """
    patterns = (
        rf"function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{",
        rf"\b{re.escape(name)}\s*=\s*function\s*\**\s*\([^)]*\)\s*\{{",
        rf"\b{re.escape(name)}\s*=\s*\([^)]*\)\s*=>\s*\{{",
    )
    for pattern in patterns:
        match = re.search(pattern, source)
        if match is None:
            continue
        start = match.end() - 1
        depth = 0
        for index in range(start, len(source)):
            char = source[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return source[start : index + 1]
    return None


def called_names(body: str) -> list[str]:
    """Identifiers invoked as ``name(`` inside a body, minus JS builtins and DOM methods."""
    names = re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", body)
    return sorted({name for name in names if name not in JS_BUILTINS})


def render_scope(page: str) -> str:
    """The render function's body plus the bodies of the page functions it calls.

    Matching the function rather than one exact line of JavaScript is deliberate: the coder may
    write the fallback inline or pull it into a helper such as ``valueText(field)``, and either
    should satisfy these tests. The expansion is transitive (a helper that calls a helper is
    still in scope), depth-limited so a mutually recursive pair cannot loop.
    """
    source = script_source(page)
    body = function_body(source, "render")
    assert body is not None, "no render() function in the page script"

    parts = [body]
    seen = {"render"}
    frontier = [body]
    for _ in range(4):  # depth limit
        next_frontier = []
        for chunk in frontier:
            for name in called_names(chunk):
                if name in seen:
                    continue
                helper = function_body(source, name)
                if helper is None:
                    continue
                seen.add(name)
                parts.append(helper)
                next_frontier.append(helper)
        if not next_frontier:
            break
        frontier = next_frontier
    return "\n/* --- */\n".join(parts)


def raw_chunk(scope: str) -> str:
    """The first part of the render scope that reads ``raw``; asserts some part does."""
    chunks = [part for part in scope.split("\n/* --- */\n") if RAW_RE.search(part)]
    assert chunks, "the render source never reads field.raw"
    return chunks[0]


def has_placeholder(source: str) -> bool:
    """True if the em-dash placeholder literal survives in ``source``, in any spelling."""
    return any(spelling in source for spelling in EM_DASH_SPELLINGS)


def tags(html: str, name: str) -> list[str]:
    """Every opening tag ``<name ...>`` in ``html``, as raw source strings."""
    return re.findall(rf"<{name}\b[^>]*>", html, flags=re.IGNORECASE)


def value_cells(page: str) -> list[str]:
    """The static ``<td data-cell="value">`` tags in the table markup."""
    return [
        tag
        for tag in tags(page, "td")
        if re.search(r"data-cell\s*=\s*[\"']value[\"']", tag, re.IGNORECASE)
    ]


# --------------------------------------------------------------------------- input factories
# Factories only: they build a client, an input payload or a scripted collaborator. None of
# them normalizes a state, parses a date, decides an issue code or formats a cell.


def found(value: str) -> models.FieldResult:
    """Factory only: a ``found`` FieldResult carrying exactly what the extractor claimed."""
    return models.FieldResult(value=value, status=models.FieldStatus.FOUND)


def make_result(**overrides: Any) -> models.ExtractionResult:
    """Factory only: an all-``found`` ExtractionResult with the named fields replaced."""
    fields: dict[str, Any] = {
        "provider_name": found("Jane Doe"),
        "license_number": found("LCSW-12345"),
        "state": found("California"),
        "expiration_date": found("2099-12-31"),
    }
    unknown = set(overrides) - set(fields)
    assert unknown == set(), f"make_result got unknown field names: {sorted(unknown)}"
    fields.update(overrides)
    return models.ExtractionResult(**fields)


def client_for(result: models.ExtractionResult) -> TestClient:
    """Factory only: a client over an app whose extractor is scripted to return ``result``."""
    return TestClient(
        create_app(
            ocr_engine=ocr.FakeOcrEngine(OCR_TEXT),
            field_extractor=extraction.FakeFieldExtractor(result),
            clock=lambda: TODAY,
        )
    )


def extract_through_the_api(result: models.ExtractionResult) -> dict[str, Any]:
    """Factory only: upload a PNG and run the real extract endpoint; return the JSON body."""
    client = client_for(result)
    upload = client.post("/documents", files={"file": ("license.png", PNG_BYTES, "image/png")})
    assert upload.status_code == 201, upload.text
    response = client.post(f"/documents/{upload.json()['id']}/extract")
    assert response.status_code == 200, response.text
    return dict(response.json())


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def client() -> TestClient:
    """Factory only: a client over a default app."""
    return TestClient(create_app())


@pytest.fixture
def page(client: TestClient) -> str:
    """The served page body, asserted non-empty so no source test passes against a 404."""
    response = client.get("/")
    assert response.status_code == 200, response.text
    assert response.text != ""
    return response.text


@pytest.fixture
def scope(page: str) -> str:
    """The render function's source, plus the helpers it calls."""
    return render_scope(page)


# --------------------------------------------------------------------------- AC1
# Source-level only: "the Value cell shows 06-11-2015" is not observable from here.


def test_ac1_value_cell_source_falls_back_to_raw(scope: str) -> None:
    """Happy path: the code filling the value cell reads ``raw`` as well as ``value``.

    Proves the fallback is *written*, not that it renders.
    """
    assert "value" in scope, "the render source does not fill the value cell"  # anchor

    assert VALUE_RE.search(scope) is not None, "the render source never reads field.value"
    assert RAW_RE.search(scope) is not None, (
        "the render source never reads field.raw, so a null value can only show a dash"
    )

    # AC1 also keeps the other two cells: status "invalid", issues "expired".
    assert re.search(r"\.status\b", scope) is not None
    assert re.search(r"\.issues\b", scope) is not None


def test_ac1_fallback_is_shared_by_every_field_row(scope: str, page: str) -> None:
    """Edge: the fallback lives on the shared render path, not special-cased to one field.

    An unknown state and an unparseable date reach the page the same way an expired date does,
    so a fallback keyed on ``expiration_date`` would fix one row of the ticket's table and
    leave the other broken.
    """
    assert RAW_RE.search(scope) is not None, "no raw fallback to check"  # anchor

    chunk = raw_chunk(scope)
    for name in models.FIELD_NAMES:
        assert f'"{name}"' not in chunk and f"'{name}'" not in chunk, (
            f"the raw fallback names {name} literally; it must apply to every field"
        )
    assert "FIELD_NAMES" in script_source(page)


def test_ac1_fallback_does_not_write_raw_back_into_the_value_property(scope: str) -> None:
    """Failure path: the page may display ``raw``, never assign it over ``value``.

    Copying raw into the payload would make the fallback indistinguishable from a confirmed
    value for anything downstream that reads the same object (AC3, AC5).
    """
    assert RAW_RE.search(scope) is not None, "no raw fallback to check"  # anchor

    assignments = re.findall(r"\.value\s*=(?!=)", scope)
    assert assignments == [], f"the render source assigns to .value: {assignments}"


# --------------------------------------------------------------------------- AC2


def test_ac2_null_value_and_null_raw_keeps_the_em_dash_placeholder(scope: str) -> None:
    """Happy path: the placeholder survives and the raw read is conditional, not unconditional.

    The fallback must not invent content when nothing was read at all.
    """
    assert has_placeholder(scope), "the em-dash placeholder is gone from the render source"

    chunk = raw_chunk(scope)
    match = RAW_RE.search(chunk)
    assert match is not None
    window = chunk[max(0, match.start() - 160) : match.end() + 160]
    assert GUARD_RE.search(window) is not None, (
        f"field.raw is read unconditionally, with no null guard near it: {window!r}"
    )


def test_ac2_marker_is_cleared_between_renders_not_only_set(page: str) -> None:
    """Edge: the function that blanks the cells also takes the marker off.

    Cells are reused across uploads. A marker that is only ever set leaves the next document's
    empty or confirmed cell wearing a stale "unconfirmed" flag.
    """
    source = script_source(page)
    assert MARKER_RE.search(source) is not None, (
        f"the page source has no {MARKER_ATTR} marker at all"
    )  # anchor

    body = function_body(source, "clearResults")
    assert body is not None, "no clearResults() function in the page script"
    assert MARKER_CLEAR_RE.search(body) is not None, (
        "clearResults does not remove the fallback marker, so it survives the next upload"
    )


def test_ac2_placeholder_path_never_prints_null_or_undefined(scope: str) -> None:
    """Failure path: with nothing read, the cell shows the dash, not a stringified null."""
    assert MARKER_RE.search(scope) is not None, "no fallback marker to check"  # anchor
    assert has_placeholder(scope)

    for literal in ('"null"', "'null'", '"undefined"', "'undefined'"):
        assert literal not in scope, f"the render source can print the literal {literal}"


# --------------------------------------------------------------------------- AC3


def test_ac3_confirmed_value_takes_precedence_over_raw(scope: str) -> None:
    """Happy path: ``value`` is consulted before ``raw`` on the value-cell path.

    Source-level ordering, not a rendered cell: a fallback chain that reached ``raw`` first
    would show the transcription even for an accepted value.
    """
    chunk = raw_chunk(scope)
    value_match = VALUE_RE.search(chunk)
    raw_match = RAW_RE.search(chunk)
    assert value_match is not None, "the value-cell source reads raw but never value"
    assert raw_match is not None
    assert value_match.start() < raw_match.start(), (
        f"raw is consulted before value in: {chunk.strip()!r}"
    )


def test_ac3_marker_is_applied_conditionally_not_to_every_cell(scope: str) -> None:
    """Edge: the render source both sets and unsets the marker, so a confirmed cell is clean."""
    assert MARKER_RE.search(scope) is not None, (
        f"the render source has no {MARKER_ATTR} marker at all"
    )  # anchor

    assert MARKER_CLEAR_RE.search(scope) is not None, (
        "the render source only ever sets the marker; a confirmed value would stay flagged"
    )
    assert GUARD_RE.search(scope) is not None


def test_ac3_fallback_does_not_touch_the_status_or_issues_cells(scope: str) -> None:
    """Failure path: the fallback changes the Value cell only.

    Status and Issues keep coming from ``status`` and ``issues``; a fallback that wrote raw
    into them would hide why the value was rejected.
    """
    assert RAW_RE.search(scope) is not None, "no raw fallback to check"  # anchor

    for kind, prop in (("status", r"\.status\b"), ("issues", r"\.issues\b")):
        assert re.search(prop, scope) is not None, f"the {kind} cell no longer reads field.{kind}"

    # The raw read must not sit in the same statement as a status or issues cell write.
    for statement in re.split(r";", raw_chunk(scope)):
        if RAW_RE.search(statement) is None:
            continue
        for kind in ("status", "issues"):
            assert f'"{kind}"' not in statement and f"'{kind}'" not in statement, (
                f"raw is written into the {kind} cell: {statement.strip()!r}"
            )


# --------------------------------------------------------------------------- AC4


def test_ac4_marker_literal_is_present_in_the_render_source(scope: str) -> None:
    """Happy path: the render source carries ``data-unconfirmed`` set to ``"true"``."""
    assert MARKER_RE.search(scope) is not None, (
        f"the render source never mentions the {MARKER_ATTR} marker"
    )

    match = MARKER_RE.search(scope)
    assert match is not None
    window = scope[match.start() : match.end() + 120]
    assert "true" in window, f'the marker is present but never set to "true" near it: {window!r}'


def test_ac4_marker_is_not_baked_into_the_static_table_markup(page: str) -> None:
    """Edge: the marker is applied by the script, never pre-printed on an empty cell.

    A cell that ships marked would flag a confirmed value the moment the first result lands.
    """
    assert MARKER_RE.search(script_source(page)) is not None, (
        f"the page script has no {MARKER_ATTR} marker at all"
    )  # anchor

    cells = value_cells(page)
    assert len(cells) == len(models.FIELD_NAMES), cells
    for tag in cells:
        assert MARKER_ATTR not in tag.lower(), f"static markup ships the marker: {tag}"


def test_ac4_marker_has_a_visible_style_not_only_an_attribute(page: str) -> None:
    """Failure path: a marker no reader can see does not satisfy "visually distinguishable".

    AC4 exists so a reader is not misled into thinking a transcription was accepted. An
    attribute that only tests can see leaves the human reading the table exactly as misled.
    """
    assert MARKER_ATTR in page.lower(), f"no {MARKER_ATTR} marker in the page"  # anchor

    css = style_source(page)
    assert css != "", "the page has no <style> block"
    assert MARKER_ATTR in css.lower(), (
        f"no CSS selector mentions {MARKER_ATTR}; the fallback looks identical to a value"
    )


# --------------------------------------------------------------------------- AC5
# The real one: the endpoint, not the page source. This is what stops a display change from
# becoming an API change.


def test_ac5_expired_date_keeps_value_null_and_raw_in_the_json() -> None:
    """Happy path: the ticket's own row -- expired date, value null, raw preserved."""
    body = extract_through_the_api(make_result(expiration_date=found(EXPIRED_DATE_RAW)))

    assert body["expiration_date"] == {
        "value": None,
        "status": "invalid",
        "raw": EXPIRED_DATE_RAW,
        "issues": ["expired"],
    }
    assert body["expiration_date"]["value"] is None
    assert body["expiration_date"]["raw"] == EXPIRED_DATE_RAW
    assert body["expiration_date"]["issues"] == ["expired"]
    assert "expiration_date" in body["missing_fields"]
    assert body["missing_fields"] == ["expiration_date"]


def test_ac5_unknown_state_keeps_value_null_and_raw_in_the_json() -> None:
    """Edge: a non-US jurisdiction behaves the same way -- value null, raw kept, still missing."""
    body = extract_through_the_api(make_result(state=found(UNKNOWN_STATE_RAW)))

    assert body["state"] == {
        "value": None,
        "status": "invalid",
        "raw": UNKNOWN_STATE_RAW,
        "issues": ["unknown_state"],
    }
    assert "state" in body["missing_fields"]
    assert body["missing_fields"] == ["state"]

    # Two fallbacks at once are still two nulls and two entries, in FIELD_NAMES order.
    both = extract_through_the_api(
        make_result(state=found(UNKNOWN_STATE_RAW), expiration_date=found(EXPIRED_DATE_RAW))
    )
    assert both["state"]["value"] is None
    assert both["expiration_date"]["value"] is None
    assert both["missing_fields"] == ["state", "expiration_date"]


def test_ac5_confirmed_fields_are_untouched_by_the_display_fallback() -> None:
    """Failure path: a confirmed field keeps its value and stays out of missing_fields.

    If T-009 is ever "implemented" by loosening SPEC A8 or by editing validate_result so raw is
    promoted into value, this test and the two above go red -- which is the point of them.
    """
    body = extract_through_the_api(make_result(expiration_date=found(EXPIRED_DATE_RAW)))

    assert body["provider_name"] == {
        "value": "Jane Doe",
        "status": "found",
        "raw": "Jane Doe",
        "issues": [],
    }
    assert body["state"]["value"] == "CA"
    assert body["license_number"]["value"] == "LCSW-12345"
    for name in ("provider_name", "license_number", "state"):
        assert name not in body["missing_fields"]

    # The response shape itself is unchanged: no new key smuggled in for the display.
    assert set(body) == set(models.FIELD_NAMES) | {
        "missing_fields",
        "document_id",
        "text_source",
    }
    for name in models.FIELD_NAMES:
        assert set(body[name]) == {"value", "status", "raw", "issues"}
