"""Application factory, health check and module-level app (T-001, T-002)."""

from __future__ import annotations

from fastapi import FastAPI

from app.api import router
from app.errors import register_error_handlers
from app.repository import DocumentRepository, InMemoryDocumentRepository
from app.settings import Settings


def create_app(
    settings: Settings | None = None,
    *,
    repository: DocumentRepository | None = None,
) -> FastAPI:
    """Build an isolated app with injected collaborators.

    ``settings`` of ``None`` means ``Settings()`` defaults, never the environment (A13).
    ``repository`` of ``None`` means a fresh ``InMemoryDocumentRepository()``.
    """
    app = FastAPI(title="Provider Document Intake")
    app.state.settings = settings if settings is not None else Settings()
    app.state.repository = repository if repository is not None else InMemoryDocumentRepository()
    register_error_handlers(app)
    app.include_router(router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app(Settings.from_env())
