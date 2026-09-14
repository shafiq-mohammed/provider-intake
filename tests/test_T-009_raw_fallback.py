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

A source-contract test is worth nothing unless it can go red, so the AC1-AC4 assertions are
structural, not textual: they read the statement that does the work (the one selecting the
displayed text, the one clearing the marker) rather than searching a character window for a
token that some unrelated line also happens to contain. Two of them were previously satisfiable
by lines with nothing to do with the fallback -- ``textContent = ""`` in ``clearResults``, and
the presence test on the line above the selection -- and so survived mutations of the code they
name. The regexes below record what those holes were.

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

# The marker named in a dataset access: ``.dataset.unconfirmed`` / ``.dataset["unconfirmed"]``.
_DATASET_MARKER = r"dataset\s*(?:\.\s*unconfirmed\b|\[\s*[\"']unconfirmed[\"']\s*\])"

# Any construct that takes *this* marker off again **and names it while doing so**:
# ``removeAttribute("data-unconfirmed")``, ``toggleAttribute("data-unconfirmed", cond)``,
# ``delete el.dataset.unconfirmed``, or ``el.dataset.unconfirmed = ""``.
#
# A bare assignment of ``""`` is deliberately not enough. The previous version of this regex
# accepted ``=\s*(null|false|""|'')`` anywhere, so ``cell(name, kind).textContent = ""`` -- a
# line that blanks a cell and has nothing to do with the marker -- satisfied it. That let the
# ``removeAttribute("data-unconfirmed")`` line be deleted from ``clearResults`` with all twelve
# tests still green, which is exactly the regression AC2 names.
MARKER_CLEAR_RE = re.compile(
    r"(?:remove|toggle)Attribute\s*\(\s*(?:[\"']data-unconfirmed[\"']|[\w$.]*[Uu]nconfirmed\b)"
    rf"|delete\s+[^;={{}}]{{0,160}}?{_DATASET_MARKER}"
    rf"|{_DATASET_MARKER}\s*=\s*(?:\"\"|''|null|undefined|false)"
)
# The mirror image: a construct that puts the marker on. ``toggleAttribute`` counts as both,
# because one call really does do both.
MARKER_SET_RE = re.compile(
    r"(?:set|toggle)Attribute\s*\(\s*[\"']data-unconfirmed[\"']"
    rf"|{_DATASET_MARKER}\s*=\s*(?![\"']\s*[\"']|''|null|undefined|false)"
)

# Constructs that genuinely make a reference conditional. Bare ``?`` and ``!`` used to be in
# here, which made "raw is read with a null guard" true of any text containing a ``!==``
# anywhere -- near enough to a tautology to be worthless. A ternary counts only when its ``:``
# branch is present too.
GUARD_RE = re.compile(
    r"[=!]==?\s*(?:null|undefined)"  # == null, === null, != undefined, !== null
    r"|\btypeof\b"
    r"|\?\?"
    r"|\|\|"
    r"|\bif\s*\("
    r"|\?[^?:]*:"  # a ternary, both branches
)

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


def statements(chunk: str) -> list[str]:
    """``chunk`` split into statements on ``;``.

    Crude on purpose, and sufficient: the render path has no ``for (;;)`` and no semicolon
    inside a string literal. Statement scope matters because a 320-character window around the
    ``raw`` read spans the whole of ``fillValueCell``, so anything asserted over a window is
    really asserted over the entire function.
    """
    return chunk.split(";")


def raw_statement(chunk: str) -> str:
    """The first statement in ``chunk`` that reads ``raw``; asserts there is one."""
    for statement in statements(chunk):
        if RAW_RE.search(statement):
            return statement
    raise AssertionError(f"no statement in the chunk reads field.raw: {chunk.strip()!r}")


def inline_locals(chunk: str, expression: str) -> str:
    """Substitute simple ``var name = expr;`` locals declared in ``chunk`` into ``expression``.

    Reader only -- it decides nothing about precedence. It exists so a condition written as
    ``confirmed ? ... : ...`` can be read back as the ``present(field.value)`` it was assigned,
    and the test does not silently depend on whether the coder named the test or inlined it.
    """
    declared = dict(re.findall(r"\bvar\s+([A-Za-z_$][\w$]*)\s*=\s*([^;]+);", chunk))
    for _ in range(3):  # depth limit: a self-referential var cannot loop forever
        expanded = expression
        for name, value in declared.items():
            expanded = re.sub(rf"\b{re.escape(name)}\b", f"({value})", expanded)
        if expanded == expression:
            break
        expression = expanded
    return expression


def _top_level_ternary(statement: str) -> tuple[str, str, str] | None:
    """Split ``cond ? consequent : alternative`` on the first ternary outside any bracket."""
    depth = 0
    question = None
    index = 0
    while index < len(statement):
        char = statement[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "?":
            if statement[index : index + 2] == "??":  # nullish coalescing, not a ternary
                index += 2
                continue
            if statement[index + 1 : index + 2] == ".":  # optional chaining
                index += 2
                continue
            if depth == 0:
                question = index
                break
        index += 1
    if question is None:
        return None

    depth = 0
    nested = 0
    index = question + 1
    while index < len(statement):
        char = statement[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "?" and depth == 0:
            if statement[index : index + 2] == "??" or statement[index + 1 : index + 2] == ".":
                index += 2
                continue
            nested += 1
        elif char == ":" and depth == 0:
            if nested:
                nested -= 1
            else:
                return (
                    statement[:question],
                    statement[question + 1 : index],
                    statement[index + 1 :],
                )
        index += 1
    return None


def _top_level_coalesce(statement: str) -> tuple[str, str] | None:
    """Split ``left ?? right`` / ``left || right`` on the first such operator outside brackets."""
    depth = 0
    for index, char in enumerate(statement):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif depth == 0 and statement[index : index + 2] in ("??", "||"):
            return statement[:index], statement[index + 2 :]
    return None


def right_of_assignment(expression: str) -> str:
    """``expression`` with any leading ``var x =`` / ``el.textContent =`` stripped off."""
    return re.split(r"=(?![=>])", expression)[-1]


def negations(expression: str) -> int:
    """Count logical ``!`` in ``expression``, ignoring the ``!`` of ``!=`` and ``!==``."""
    return len(re.findall(r"!(?!=)", expression))


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

    # Scoped to the statement that reads raw, not a 320-character window around it: the window
    # covered the whole of fillValueCell, so any guard anywhere in the function satisfied it.
    statement = raw_statement(raw_chunk(scope))
    assert GUARD_RE.search(statement) is not None, (
        f"field.raw is read unconditionally, with no null guard in the statement: {statement!r}"
    )


def test_ac2_marker_is_cleared_between_renders_not_only_set(page: str) -> None:
    """Edge: the function that blanks the cells also takes the marker off, by name.

    Cells are reused across uploads. A marker that is only ever set leaves the next document's
    cell wearing a stale flag: an upload that fails after ``clearResults`` but before ``render``
    shows " (as read)" beside an empty Value cell.

    The clearing construct must name the marker. ``clearResults`` already blanks cells with
    ``cell(name, kind).textContent = ""``, so "an assignment of an empty string happens
    somewhere in this function" is true whether or not the marker is ever cleared.
    """
    source = script_source(page)
    assert MARKER_RE.search(source) is not None, (
        f"the page source has no {MARKER_ATTR} marker at all"
    )  # anchor

    body = function_body(source, "clearResults")
    assert body is not None, "no clearResults() function in the page script"
    assert MARKER_CLEAR_RE.search(body) is not None, (
        "clearResults does not remove the fallback marker by name, so it survives the next "
        f'upload. Expected one of removeAttribute("{MARKER_ATTR}"), '
        'delete <el>.dataset.unconfirmed, or <el>.dataset.unconfirmed = "" in: '
        f"{body.strip()!r}"
    )


def test_ac2_placeholder_path_never_prints_null_or_undefined(scope: str) -> None:
    """Failure path: with nothing read, the cell shows the dash, not a stringified null."""
    assert MARKER_RE.search(scope) is not None, "no fallback marker to check"  # anchor
    assert has_placeholder(scope)

    for literal in ('"null"', "'null'", '"undefined"', "'undefined'"):
        assert literal not in scope, f"the render source can print the literal {literal}"


# --------------------------------------------------------------------------- AC3


def test_ac3_confirmed_value_takes_precedence_over_raw(scope: str) -> None:
    """Happy path: the branch taken when ``value`` is present yields ``value``, never ``raw``.

    Textual ordering over the whole chunk proved nothing and is gone. The first ``.value`` in
    ``fillValueCell`` is the presence *test* on the line above the selection, so
    ``present(field.value) ? field.raw : field.value`` -- precedence flipped, the bug this test
    is named for -- also has value before raw and passed the old assertion.

    So this reads the one statement that selects the displayed text, splits it into condition,
    consequent and alternative, resolves the condition back through any local it was named into
    (``confirmed`` -> ``present(field.value)``) and asserts the branch reached when the value is
    present is the one carrying ``value``. Still source, not a rendered cell -- a jsdom test is
    the only thing that proves the DOM -- but it now goes red when the branches swap.
    """
    chunk = raw_chunk(scope)

    selections = []
    for statement in statements(chunk):
        if RAW_RE.search(statement) is None:
            continue
        ternary = _top_level_ternary(statement)
        if ternary is not None:
            selections.append((statement, ternary))
            continue
        coalesce = _top_level_coalesce(statement)
        if coalesce is not None:
            left, right = coalesce
            selections.append((statement, (left, left, right)))

    assert selections, (
        "no statement on the value-cell path picks between value and raw with a ternary or a "
        "?? / || chain, so this test cannot read which one wins. If the selection was "
        "refactored into if/else, prove precedence in the jsdom test rather than loosening "
        f"this one: {chunk.strip()!r}"
    )

    for statement, (condition, consequent, alternative) in selections:
        condition = inline_locals(chunk, right_of_assignment(condition))
        assert VALUE_RE.search(condition) is not None, (
            f"the branch that picks value or raw is not conditioned on value: {statement!r}"
        )
        assert RAW_RE.search(condition) is None, (
            "the branch that picks value or raw is conditioned on raw, so which one wins is "
            f"not readable from the source; prove it in the jsdom test: {statement!r}"
        )

        # An odd number of logical ! means the condition reads "value is absent", which puts
        # the value branch on the other side. Either spelling is fine; swapping is not.
        value_present, value_absent = consequent, alternative
        if negations(condition) % 2:
            value_present, value_absent = alternative, consequent

        assert VALUE_RE.search(value_present) is not None, (
            f"when the value is present the cell does not show value: {statement!r}"
        )
        assert RAW_RE.search(value_present) is None, (
            f"raw is shown on the branch taken when the value is present: {statement!r}"
        )
        assert RAW_RE.search(value_absent) is not None, (
            f"the branch taken when the value is absent does not fall back to raw: {statement!r}"
        )


def test_ac3_marker_is_applied_conditionally_not_to_every_cell(scope: str) -> None:
    """Edge: the render source both sets and unsets the marker, so a confirmed cell is clean.

    Both halves are required in the same chunk that reads raw: a render that only ever calls
    ``setAttribute`` leaves a cell flagged from the previous document even when this one's value
    was accepted. ``toggleAttribute("data-unconfirmed", cond)`` satisfies both, since one call
    really does do both.
    """
    assert MARKER_RE.search(scope) is not None, (
        f"the render source has no {MARKER_ATTR} marker at all"
    )  # anchor

    chunk = raw_chunk(scope)
    assert MARKER_SET_RE.search(chunk) is not None, (
        f"the value-cell path never sets the {MARKER_ATTR} marker: {chunk.strip()!r}"
    )
    assert MARKER_CLEAR_RE.search(chunk) is not None, (
        "the value-cell path only ever sets the marker; a confirmed value would keep the flag "
        f"left on the cell by the previous render: {chunk.strip()!r}"
    )


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
