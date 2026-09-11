"""API router: upload limits, document upload, metadata and text extraction (T-002, T-003)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, File, Request, UploadFile

from app.errors import ApiError
from app.models import DocumentMeta
from app.ocr import OcrEngine, OcrError
from app.repository import DocumentRepository, StoredDocument
from app.settings import Settings
from app.text_extraction import extract_text
from app.uploads import validate_upload

router = APIRouter()


@router.get("/config")
async def read_config(request: Request) -> dict[str, Any]:
    """Report the upload limits a client must respect."""
    settings: Settings = request.app.state.settings
    return {
        "max_upload_bytes": settings.max_upload_bytes,
        "allowed_content_types": sorted(settings.allowed_content_types),
    }


@router.post("/documents", response_model=DocumentMeta, status_code=201)
async def upload_document(request: Request, file: Annotated[UploadFile, File()]) -> DocumentMeta:
    """Store one uploaded document and return its metadata."""
    settings: Settings = request.app.state.settings
    repository: DocumentRepository = request.app.state.repository

    content = await file.read()
    validate_upload(content, file.content_type, settings)

    stored = repository.add(
        filename=file.filename or "",
        content_type=file.content_type or "",
        content=content,
    )
    return stored.meta()


@router.get("/documents/{document_id}", response_model=DocumentMeta)
async def read_document(request: Request, document_id: str) -> DocumentMeta:
    """Return the metadata of a previously uploaded document."""
    return _require_document(request, document_id).meta()


@router.post("/documents/{document_id}/text")
async def extract_document_text(request: Request, document_id: str) -> dict[str, Any]:
    """Return the document's raw text and where it came from; results are never cached."""
    settings: Settings = request.app.state.settings
    engine: OcrEngine = request.app.state.ocr_engine

    document = _require_document(request, document_id)
    try:
        extraction = extract_text(document, engine, min_pdf_text_chars=settings.min_pdf_text_chars)
    except OcrError as exc:
        raise ApiError(
            422,
            "ocr_failed",
            "The OCR engine could not read this document.",
            {"reason": type(exc.__cause__ or exc).__name__},
        ) from exc

    return {
        "document_id": document.id,
        "text": extraction.text,
        "source": extraction.source,
        "char_count": extraction.char_count,
    }


def _require_document(request: Request, document_id: str) -> StoredDocument:
    """Return the stored document or raise the 404 envelope."""
    repository: DocumentRepository = request.app.state.repository

    stored = repository.get(document_id)
    if stored is None:
        raise ApiError(
            404,
            "document_not_found",
            "No document with that id.",
            {"document_id": document_id},
        )
    return stored
