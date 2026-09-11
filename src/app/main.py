"""Application factory, health check and module-level app (T-001)."""

from __future__ import annotations

from fastapi import FastAPI

from app.errors import register_error_handlers
from app.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an isolated app.

    ``settings`` of ``None`` means ``Settings()`` defaults, never the environment (A13).
    """
    app = FastAPI(title="Provider Document Intake")
    app.state.settings = settings if settings is not None else Settings()
    register_error_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app(Settings.from_env())
