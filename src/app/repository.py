"""Document storage: StoredDocument, the repository protocol and the in-memory one (T-002)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from app.models import DocumentMeta


@dataclass(frozen=True)
class StoredDocument:
    """One uploaded document. ``content`` is kept out of ``repr`` (SPEC section 5)."""

    id: str
    filename: str
    content_type: str
    content: bytes = field(repr=False)

    @property
    def size_bytes(self) -> int:
        """Size of the stored bytes."""
        return len(self.content)

    def meta(self) -> DocumentMeta:
        """Public metadata for this document."""
        return DocumentMeta(
            id=self.id,
            filename=self.filename,
            content_type=self.content_type,
            size_bytes=self.size_bytes,
        )


class DocumentRepository(Protocol):
    """Storage for uploaded documents."""

    def add(self, *, filename: str, content_type: str, content: bytes) -> StoredDocument:
        """Store the document and return it."""
        ...

    def get(self, document_id: str) -> StoredDocument | None:
        """Return the document with this id, or ``None`` when there is none."""
        ...


class InMemoryDocumentRepository:
    """A process-memory repository, one instance per app."""

    def __init__(self) -> None:
        self._documents: dict[str, StoredDocument] = {}

    def add(self, *, filename: str, content_type: str, content: bytes) -> StoredDocument:
        """Generate a uuid4 id, store the document and return it."""
        document = StoredDocument(
            id=str(uuid.uuid4()),
            filename=filename,
            content_type=content_type,
            content=content,
        )
        self._documents[document.id] = document
        return document

    def get(self, document_id: str) -> StoredDocument | None:
        """Return the document with this id, or ``None`` when there is none."""
        return self._documents.get(document_id)

    def __len__(self) -> int:
        return len(self._documents)
