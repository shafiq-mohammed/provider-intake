"""Pydantic models for request/response shapes (T-002, T-003)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class DocumentMeta(BaseModel):
    """Public metadata for a stored document; never carries the bytes."""

    id: str
    filename: str
    content_type: str
    size_bytes: int


class TextExtraction(BaseModel):
    """The raw text of a document and where it came from."""

    text: str
    source: Literal["pdf_text_layer", "ocr"]
    char_count: int
