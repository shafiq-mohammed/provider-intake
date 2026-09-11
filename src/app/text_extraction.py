"""PDF-first text extraction with the OCR engine as the fallback (T-003)."""

from __future__ import annotations

import io

import pypdf

from app.models import TextExtraction
from app.ocr import OcrEngine, OcrError
from app.repository import StoredDocument

PDF_CONTENT_TYPE = "application/pdf"


def pdf_text_layer(content: bytes) -> str:
    """Join the text layer of every page with newlines.

    Any pypdf failure (corrupt, encrypted or empty bytes) is an empty text layer,
    never an exception (SPEC A6).
    """
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() for page in reader.pages)
    except Exception:
        return ""


def extract_text(
    document: StoredDocument, engine: OcrEngine, *, min_pdf_text_chars: int
) -> TextExtraction:
    """Return the document's text, from the PDF text layer when it is substantial.

    A PDF whose text layer holds at least ``min_pdf_text_chars`` stripped characters is
    answered without calling the engine. Everything else (thin or unreadable layers, and
    every image type) goes to the engine, whose failures become ``OcrError``.
    """
    if document.content_type == PDF_CONTENT_TYPE:
        text = pdf_text_layer(document.content)
        if len(text.strip()) >= min_pdf_text_chars:
            return TextExtraction(text=text, source="pdf_text_layer", char_count=len(text))

    try:
        text = engine.extract_text(document.content, document.content_type)
    except Exception as exc:
        raise OcrError("The OCR engine failed.") from exc
    return TextExtraction(text=text, source="ocr", char_count=len(text))
