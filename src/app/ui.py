"""Static UI router: the one hand-written upload page, served at ``GET /`` (T-006)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

INDEX_PATH: Path = Path(__file__).parent / "static" / "index.html"

router = APIRouter()


@router.get("/", include_in_schema=False)
async def read_index() -> FileResponse:
    """Serve ``static/index.html`` verbatim; the page fetches its own config at load."""
    return FileResponse(INDEX_PATH, media_type="text/html")
