# Provider Document Intake

A behavioral health provider uploads a credential document (jpg, png or pdf, max 10 MB). The
service extracts the raw text, asks an LLM to map it onto four fields, validates and normalizes
that answer deterministically, and shows what was found — and what could not be reliably
determined, and why.

![The UI after extracting a real licence: four fields with value, status and issues, plus the missing-fields line](docs/images/ui-extraction.png)

That screenshot is a real end-to-end run against the Anthropic API — vision OCR, then field
extraction, then deterministic validation. Note the `inferred_from_address` issue on the state
row: the model reported that itself, and issue strings it invents are preserved rather than
discarded, alongside the four codes the validator sets.

## Quickstart

```bash
uv pip install -e '.[dev]'      # or: pip install -e '.[dev]'
python -m pytest -q             # 239 passed, 1 skipped
PYTHONPATH=src uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 and upload a document. With no configuration the service runs **fully
offline** against in-memory fakes — no API key, no network. That is the default, and the entire
test suite runs that way.

To use real Claude for OCR and extraction:

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env     # gitignored; see docs/ENVIRONMENT.md
APP_OCR_BACKEND=anthropic APP_EXTRACTOR_BACKEND=anthropic \
  PYTHONPATH=src uvicorn app.main:app --reload
```

Unset those two variables to go back to the fakes. No code change — that is the point of the
protocol boundary.

## How uncertainty is represented

Every field is a `FieldResult`, never a bare value:

```json
{
  "state":           {"value": "MD", "status": "found",   "raw": "Maryland", "issues": ["inferred_from_address"]},
  "expiration_date": {"value": null, "status": "invalid", "raw": "01/31/2020", "issues": ["expired"]},
  "missing_fields":  ["expiration_date"]
}
```

`status` is one of `found` / `not_found` / `unreadable` / `invalid`. Two rules hold throughout:

- **`value` is `null` unless `status` is `found`.** A value attached to an uncertain field is
  dropped, never returned. `FieldResult` is frozen and enforces this at construction.
- **Validation may only downgrade certainty, never raise it.** The LLM's output is untrusted
  input; deterministic post-processing can turn `found` into `invalid`, but nothing can promote a
  field the model was unsure about. Tested over the full status × field × value cross product.

So an expired licence reads correctly but is not usable: `invalid`, issue `expired`, `value` null,
with the original date still visible in `raw`.

## Pipeline

```
POST /documents        upload, validate type/size/signature, store in memory
POST /documents/{id}/text     pypdf text layer, falling back to OCR when thin or absent
POST /documents/{id}/extract  OCR -> LLM extraction -> deterministic validation
GET  /                 the upload page
GET  /config           limits, so the page can state them before you choose a file
```

Both the OCR engine and the field extractor sit behind protocols with fakes, so the whole flow
runs without network access or an API key. Errors return a JSON envelope with a 4xx status —
never a 500 for bad input — and document contents never appear in a response body, an error
detail, or a log.

## Layout

| Path | What |
|---|---|
| `src/app/` | application code |
| `tests/` | pytest, one file per ticket |
| `docs/SPEC.md` | architecture, conventions, and the interface for every ticket |
| `docs/ENVIRONMENT.md` | every environment variable, and where to put your key |
| `tasks/T-00X.md` | one ticket per vertical slice |
| `.claude/`, `scripts/` | the agentic delivery pipeline itself |

## Development

Built ticket by ticket through a test-first pipeline: plan → failing tests → implementation →
independent review → PR. Tests are derived from each ticket's acceptance criteria and committed
failing, before the implementation exists.

```bash
python -m pytest -q          # tests
ruff check .                 # lint
```

The two live tests in `tests/test_T-007_anthropic_adapters.py` skip unless `ANTHROPIC_API_KEY` is
set, and the live OCR test additionally needs a sample document at
`tests/fixtures/sample_license.png` (gitignored — real credential documents must not be committed).

## Known limitations

- The upload limit is 10 MB but the Anthropic API caps images at 5 MB, so a file in between is
  accepted and then fails as a 422 `ocr_failed`.
- Extraction errors report only the exception class name, so an operator cannot tell an auth
  failure from a rate limit without inspecting `__cause__`.
- The page's JavaScript is verified by hand, not in CI.
- Not implemented, by design: authentication, persistence, batch uploads, a human review queue,
  retries and rate limiting.
