# Environment variables

Every variable read by `Settings.from_env()` (`src/app/settings.py`). Unset keys keep their
defaults; invalid values raise `pydantic.ValidationError` at startup.

> **The app does not load `.env` files.** `Settings.from_env()` reads `os.environ` and nothing
> else — that was scoped out deliberately in T-001 ("Reading `.env` files; only process
> environment via `from_env`"). A `.env` file will sit there doing nothing unless `python-dotenv`
> is added and wired in, which is a spec change rather than configuration.

## Where to put the key

Pick one. Never commit a real key; `.env`, `.env.*` and `secrets/` are gitignored.

```bash
# One-off, current shell only — nothing on disk:
export ANTHROPIC_API_KEY="sk-ant-..."

# Persistent. Use ~/.zshenv, NOT ~/.zshrc: .zshrc is only sourced by interactive
# shells, so scripts, editors and tooling will not see the variable.
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.zshenv

# A file you source yourself, if you prefer one place for all vars:
set -a && . ./.env && set +a
```

Quote the value. An unquoted `export KEY=sk-ant-...` truncates at the first shell metacharacter,
which produces a confusing `401 API key is invalid` rather than an obvious error.

If a key is ever exposed — a transcript, a commit, a shared log — revoke it at
console.anthropic.com and issue a new one. Do not assume it went unnoticed.

## Anthropic

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Required only when a backend below is `anthropic`. Without it `create_app` raises `ValueError` at startup. |
| `APP_ANTHROPIC_MODEL` | `claude-sonnet-5` | Model id used by both adapters. |

## Backend selection

| Variable | Default | Notes |
|---|---|---|
| `APP_OCR_BACKEND` | `fake` | `fake` \| `anthropic`. `fake` runs fully offline with no key. |
| `APP_EXTRACTOR_BACKEND` | `fake` | `fake` \| `anthropic`. |

The two are independent — running real OCR with a fake extractor (or the reverse) is supported.

```bash
# Real end-to-end:
APP_OCR_BACKEND=anthropic APP_EXTRACTOR_BACKEND=anthropic uvicorn app.main:app

# Back to offline: unset both (or set them to fake). No code change.
```

## Limits

| Variable | Default | Notes |
|---|---|---|
| `APP_MAX_UPLOAD_BYTES` | `10485760` (10 MB) | **The Anthropic API caps images at 5 MB**, so a file between 5 and 10 MB is accepted by `POST /documents` and then fails as a 422 `ocr_failed` against the real backend. Lower this, or downscale before upload. |
| `APP_MIN_PDF_TEXT_CHARS` | `20` | A PDF text layer with fewer non-whitespace characters is treated as too thin and falls back to OCR. |

`allowed_content_types` is intentionally not configurable from the environment.

## Logging

`ANTHROPIC_LOG=info` is safe for request logging. **Never use `ANTHROPIC_LOG=debug` in a shared or
persisted environment** — it logs request bodies, which include base64-encoded document data, and
would put document contents in the logs (SPEC A17 forbids exactly that).

The SDK also reads `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_CUSTOM_HEADERS`
outside of `Settings`. They silently change where requests go; check them if something unexpected
happens.
