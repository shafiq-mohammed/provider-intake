"""Pydantic models for request/response shapes (T-002)."""

from __future__ import annotations

from pydantic import BaseModel


class DocumentMeta(BaseModel):
    """Public metadata for a stored document; never carries the bytes."""

    id: str
    filename: str
    content_type: str
    size_bytes: int
