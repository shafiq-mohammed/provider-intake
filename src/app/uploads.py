"""Upload validation: magic-byte signatures and the ordered upload checks (T-002)."""

from __future__ import annotations

from app.errors import ApiError
from app.settings import Settings

SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
}


def content_matches_type(content: bytes, content_type: str) -> bool:
    """True if ``content`` starts with any signature registered for ``content_type``."""
    signatures = SIGNATURES.get(content_type)
    if not signatures:
        return False
    return any(content.startswith(signature) for signature in signatures)


def validate_upload(content: bytes, content_type: str | None, settings: Settings) -> None:
    """Validate one upload; raise ``ApiError`` on the first failing check.

    Order: declared type -> size -> empty -> signature (SPEC section 9).
    """
    if content_type is None or content_type not in settings.allowed_content_types:
        raise ApiError(
            415,
            "unsupported_media_type",
            "Unsupported content type.",
            {
                "content_type": content_type,
                "allowed_content_types": sorted(settings.allowed_content_types),
            },
        )
    if len(content) > settings.max_upload_bytes:
        raise ApiError(
            413,
            "file_too_large",
            "Uploaded file is larger than the allowed maximum.",
            {"max_upload_bytes": settings.max_upload_bytes, "size_bytes": len(content)},
        )
    if len(content) == 0:
        raise ApiError(400, "empty_file", "Uploaded file is empty.")
    if not content_matches_type(content, content_type):
        raise ApiError(
            415,
            "content_type_mismatch",
            "File content does not match the declared content type.",
            {"content_type": content_type},
        )
