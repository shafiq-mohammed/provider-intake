"""ASGI entrypoint: the one place a .env file is read.

Run with ``uvicorn app.asgi:app``.

Why this module exists rather than loading .env in ``app.main``: importing ``app.main``
must not mutate ``os.environ``. The test suite imports it, and the project requires every
test to run without network access or an API key. Loading .env at that import silently put
a real key into the environment, which un-skipped the live tests and billed the caller for
a plain ``pytest`` run. Keeping the load here means only an explicit server start reads it.

Real environment variables take precedence over .env (``override=False``).
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv(override=False)

from app.main import create_app  # noqa: E402  (must follow load_dotenv)
from app.settings import Settings  # noqa: E402

app = create_app(Settings.from_env())
