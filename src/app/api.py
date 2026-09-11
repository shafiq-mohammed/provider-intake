"""API router: uploads, metadata, text extraction and field extraction (T-002..T-004)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, File, Request, UploadFile

from app.errors import ApiError
from app.extraction import ExtractorError, FieldExtractor
from app.models import (
    DocumentMeta,
    ExtractionResponse,
    ExtractionResult,
    FieldStatus,
    TextExtraction,
)
from app.ocr import OcrEngine, OcrError
from app.repository import DocumentRepository, StoredDocument
from app.settings import Settings
from app.text_extraction import extract_text
from app.uploads import validate_upload
from app.validation import validate_result

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
    extraction = _extract_text_or_422(
        document, engine, min_pdf_text_chars=settings.min_pdf_text_chars
    )

    return {
        "document_id": document.id,
        "text": extraction.text,
        "source": extraction.source,
        "char_count": extraction.char_count,
    }


@router.post("/documents/{document_id}/extract", response_model=ExtractionResponse)
async def extract_document_fields(request: Request, document_id: str) -> ExtractionResponse:
    """Return the four credential fields of a document; results are never stored."""
    settings: Settings = request.app.state.settings
    engine: OcrEngine = request.app.state.ocr_engine
    extractor: FieldExtractor = request.app.state.field_extractor
    clock: Callable[[], date] = request.app.state.clock

    document = _require_document(request, document_id)
    extraction = _extract_text_or_422(
        document, engine, min_pdf_text_chars=settings.min_pdf_text_chars
    )

    if extraction.text.strip() == "":
        # Blank text short-circuits: the extractor is never called (SPEC A7).
        result = ExtractionResult.all_with(FieldStatus.NOT_FOUND, issues=["no_text"])
    else:
        result = _extract_fields_or_422(extractor, extraction.text)

    # Deterministic normalization and downgrade-only validation (SPEC A8, T-005). The
    # ``no_text`` result passes through too: with nothing ``found`` it is a no-op.
    validated = validate_result(result, today=clock())

    return ExtractionResponse(
        document_id=document.id,
        text_source=extraction.source,
        **validated.fields(),
    )


def _extract_text_or_422(
    document: StoredDocument, engine: OcrEngine, *, min_pdf_text_chars: int
) -> TextExtraction:
    """Extract the document's text, mapping an ``OcrError`` onto the 422 envelope.

    Shared by ``/text`` and ``/extract`` so the mapping exists exactly once.
    """
    try:
        return extract_text(document, engine, min_pdf_text_chars=min_pdf_text_chars)
    except OcrError as exc:
        raise ApiError(
            422,
            "ocr_failed",
            "The OCR engine could not read this document.",
            {"reason": _reason(exc)},
        ) from exc


def _extract_fields_or_422(extractor: FieldExtractor, text: str) -> ExtractionResult:
    """Run the extractor, mapping any failure onto the 422 envelope."""
    try:
        return _run_extractor(extractor, text)
    except ExtractorError as exc:
        raise ApiError(
            422,
            "extraction_failed",
            "The field extractor could not read this document.",
            {"reason": _reason(exc)},
        ) from exc


def _run_extractor(extractor: FieldExtractor, text: str) -> ExtractionResult:
    """Call the extractor, wrapping any failure as ``ExtractorError`` (original as cause)."""
    try:
        return extractor.extract_fields(text)
    except Exception as exc:
        raise ExtractorError("The field extractor failed.") from exc


def _reason(exc: Exception) -> str:
    """Name the class that failed, and nothing else.

    Never the exception message, the document bytes or the extracted text (SPEC A17).
    """
    return type(exc.__cause__ or exc).__name__


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
