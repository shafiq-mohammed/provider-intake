"""Tests for T-006: the upload UI page served at ``GET /``.

Scope note, deliberately narrow: these tests assert the *structural contract* of the HTML that
the server hands out. No browser, no JS engine, no DOM library -- plain substring and regex
assertions on ``response.text``. They can prove that the markup hooks the page's script and the
API agree on (ids, attributes, row order, endpoint literals) are present; they cannot prove the
inline JavaScript actually runs, wires those hooks to those endpoints, or renders anything.

Apps are built with ``create_app()``; the module-level ``app`` is never imported (SPEC A13 /
section 10). No network, no API key.
"""

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import models
from app.main import create_app

# --------------------------------------------------------------------------- helpers

ALLOWED_ACCEPT = ["application/pdf", "image/jpeg", "image/png"]

TAG_RE_TEMPLATE = r"<{name}\b[^>]*>"


def index_path() -> Path:
    """Return ``app.ui.INDEX_PATH``.

    Imported inside the function on purpose: ``app.ui`` does not exist yet, and a module-level
    import would turn the whole file into a single collection error instead of one red test per
    acceptance criterion. The import form (``from app import ui``) is the same one the file will
    use once the module lands, so the import sort does not move.
    """
    from app import ui

    return ui.INDEX_PATH


def tags(html: str, name: str) -> list[str]:
    """Every opening tag ``<name ...>`` in ``html``, as raw source strings."""
    return re.findall(TAG_RE_TEMPLATE.format(name=name), html, flags=re.IGNORECASE)


def attr(tag: str, name: str) -> str | None:
    """The value of attribute ``name`` in one opening tag, or None if it is not present.

    Quote style and whitespace around ``=`` are accepted in any form, and attributes may appear
    in any order: the HTML contract is about which attributes a tag carries, not their spelling
    order.
    """
    match = re.search(rf"\b{re.escape(name)}\s*=\s*(\"([^\"]*)\"|'([^']*)')", tag, re.IGNORECASE)
    if match is None:
        return None
    return match.group(2) if match.group(2) is not None else match.group(3)


def has_boolean_attr(tag: str, name: str) -> bool:
    """True if a boolean attribute is present, bare (``disabled``) or valued (``disabled=""``)."""
    return re.search(rf"\b{re.escape(name)}\b", tag, re.IGNORECASE) is not None


def tag_with_attr(html: str, name: str, attr_name: str, value: str) -> str | None:
    """The first ``<name ...>`` tag whose ``attr_name`` equals ``value``."""
    for tag in tags(html, name):
        if attr(tag, attr_name) == value:
            return tag
    return None


def assert_envelope(body: Any, *, code: str) -> None:
    """Assert the SPEC section 9 error envelope shape."""
    assert isinstance(body, dict)
    assert list(body.keys()) == ["error"]
    error = body["error"]
    assert set(error.keys()) == {"code", "message", "details"}
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"] != ""
    assert isinstance(error["details"], dict)


# --------------------------------------------------------------------------- fixtures
# Factories only: they build a client and fetch the page. They never construct HTML.


@pytest.fixture
def client() -> TestClient:
    """Factory only: a client over a default app."""
    return TestClient(create_app())


@pytest.fixture
def page(client: TestClient) -> str:
    """The served page body. Asserts it was actually served, so no inspection test can pass
    vacuously against a 404 error envelope."""
    response = client.get("/")
    assert response.status_code == 200, response.text
    assert response.text != ""
    return response.text


# --------------------------------------------------------------------------- AC1


def test_ac1_get_root_serves_the_index_html_file(client: TestClient) -> None:
    """Happy path: 200 text/html whose body is exactly the file at ui.INDEX_PATH."""
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")

    expected = index_path().read_text(encoding="utf-8")
    assert expected != ""
    assert response.text == expected


def test_ac1_content_type_declares_utf8_charset(client: TestClient) -> None:
    """Edge: the media type carries an explicit charset, so non-ASCII text renders correctly."""
    response = client.get("/")

    assert response.status_code == 200
    content_type = response.headers["content-type"].lower()
    assert content_type.startswith("text/html")
    assert "charset=utf-8" in content_type.replace(" ", "")


def test_ac1_post_root_returns_405_envelope_not_html(client: TestClient) -> None:
    """Failure path: / is GET only; POST is the JSON envelope, never the page."""
    response = client.post("/")

    assert response.status_code == 405
    assert response.headers["content-type"].startswith("application/json")
    assert_envelope(response.json(), code="method_not_allowed")
    assert "<html" not in response.text.lower()


# --------------------------------------------------------------------------- AC2


def test_ac2_file_input_declares_type_id_name_and_accept(page: str) -> None:
    """Happy path: one <input> tag carries all four attributes, in any order."""
    file_inputs = [tag for tag in tags(page, "input") if attr(tag, "type") == "file"]
    assert len(file_inputs) == 1, file_inputs
    tag = file_inputs[0]

    assert attr(tag, "id") == "file"
    assert attr(tag, "name") == "file"

    accept = attr(tag, "accept")
    assert accept is not None
    assert [part.strip() for part in accept.split(",")] == ALLOWED_ACCEPT


def test_ac2_file_input_is_required(page: str) -> None:
    """Edge: the same input tag is marked required, so an empty submit never reaches the API."""
    tag = tag_with_attr(page, "input", "id", "file")
    assert tag is not None, 'no <input id="file"> in the page'
    assert attr(tag, "type") == "file"
    assert has_boolean_attr(tag, "required")


def test_ac2_upload_button_is_disabled_in_the_markup(page: str) -> None:
    """Happy path: the submit button ships disabled; only /config may enable it."""
    tag = tag_with_attr(page, "button", "id", "upload")
    assert tag is not None, 'no <button id="upload"> in the page'
    assert attr(tag, "type") == "submit"
    assert has_boolean_attr(tag, "disabled")


def test_ac2_page_has_exactly_one_file_input(page: str) -> None:
    """Failure path: a second file picker would make `#file` ambiguous for the script."""
    assert attr(tag_with_attr(page, "input", "id", "file") or "", "type") == "file"

    file_inputs = [tag for tag in tags(page, "input") if attr(tag, "type") == "file"]
    assert len(file_inputs) == 1, file_inputs
    assert len(re.findall(r"id\s*=\s*[\"']file[\"']", page)) == 1


# --------------------------------------------------------------------------- AC3


def test_ac3_page_states_the_limit_it_fetches_from_config(page: str) -> None:
    """Happy path: the limit paragraph exists and the script names /config and the field."""
    assert tag_with_attr(page, "p", "id", "max-upload") is not None

    assert re.search(r"[\"'`]/config[\"'`]", page) is not None, "no quoted /config literal"
    assert "max_upload_bytes" in page


def test_ac3_error_paragraph_is_an_alert_region(page: str) -> None:
    """Edge: the error paragraph is announced, since it carries client-side and API errors."""
    tag = tag_with_attr(page, "p", "id", "error")
    assert tag is not None, 'no <p id="error"> in the page'
    assert attr(tag, "role") == "alert"


def test_ac3_page_does_not_hardcode_the_upload_limit(page: str) -> None:
    """Failure path: the number must come from /config, not from the markup.

    A page that prints "10 MB" from a literal would satisfy AC3's positive assertions while
    lying to the user whenever settings.max_upload_bytes is not the default.
    """
    assert "max_upload_bytes" in page  # anchor: this really is the page under test

    assert "10485760" not in page
    assert re.search(r"10\s*mb", page, re.IGNORECASE) is None


# --------------------------------------------------------------------------- AC4


def test_ac4_result_rows_appear_in_field_names_order(page: str) -> None:
    """Happy path: the four rows exist exactly once each, in FIELD_NAMES order."""
    assert tag_with_attr(page, "table", "id", "results") is not None

    positions = []
    for name in models.FIELD_NAMES:
        marker = f'data-field="{name}"'
        assert page.count(marker) == 1, f"{marker} appears {page.count(marker)} times"
        positions.append(page.index(marker))

    assert positions == sorted(positions), dict(zip(models.FIELD_NAMES, positions, strict=True))


def test_ac4_each_row_has_value_status_and_issues_cells(page: str) -> None:
    """Happy path: every field row carries the three cells the script fills in."""
    for name in models.FIELD_NAMES:
        row = re.search(
            rf'<tr\b[^>]*data-field="{name}"[^>]*>(.*?)</tr>', page, re.IGNORECASE | re.DOTALL
        )
        assert row is not None, f'no <tr data-field="{name}"> ... </tr> block'
        body = row.group(1)
        for cell in ("value", "status", "issues"):
            tag = tag_with_attr(body, "td", "data-cell", cell)
            assert tag is not None, f'row {name} has no <td data-cell="{cell}">'


def test_ac4_page_names_both_api_calls_it_makes(page: str) -> None:
    """Happy path: the upload and the extract endpoints both appear in the page source."""
    assert re.search(r"[\"'`]/documents[\"'`]", page) is not None, "no quoted /documents literal"
    assert "/extract" in page


def test_ac4_missing_fields_and_document_meta_paragraphs_exist(page: str) -> None:
    """Edge: the two summary paragraphs outside the table are present."""
    assert tag_with_attr(page, "p", "id", "missing-fields") is not None
    assert tag_with_attr(page, "p", "id", "document-meta") is not None


def test_ac4_page_loads_no_external_assets(page: str) -> None:
    """Failure path: no external script, stylesheet, or absolute URL in src/href.

    The page must work offline against its own origin; an external asset is also an unreviewed
    third party in the upload path.
    """
    assert 'id="results"' in page  # anchor: this really is the page under test

    assert re.search(r"<script\b[^>]*\bsrc\s*=", page, re.IGNORECASE) is None
    for tag in tags(page, "link"):
        assert attr(tag, "rel") != "stylesheet", tag
    assert re.search(r"\b(src|href)\s*=\s*[\"']\s*https?://", page, re.IGNORECASE) is None
