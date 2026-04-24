"""Core types for the blob-storage abstraction — Phase 10.A."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

from docstats.scope import Scope

# ---- Allow-list ---------------------------------------------------------------

# Conservative allow-list.  Anything outside this set bounces at the route
# boundary.  MIME is sniffed against the bytes (NOT trusted from the client's
# Content-Type), so the list maps to what ``mime.sniff_mime`` can positively
# identify.
ALLOWED_MIME_TYPES: Final[frozenset[str]] = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/tiff",
        # DOCX / XLSX / PPTX share the zip magic; sniff_mime() narrows via the
        # Office Content_Types element.  DOCX is the only one we currently
        # attach in practice — XLSX/PPTX are off the allow-list until product
        # decides otherwise.
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)

MAX_UPLOAD_BYTES: Final[int] = 50 * 1024 * 1024  # 50 MB

# Suffix map used when writing — derives the object path's file extension
# from the sniffed MIME so downstream tooling can MIME-infer without the DB
# row.
MIME_TO_SUFFIX: Final[dict[str, str]] = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/tiff": "tif",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}


# ---- Errors -------------------------------------------------------------------


class StorageFileError(Exception):
    """Base class for blob-storage errors.  Route layer maps to 500/502."""


class FileNotFoundInBackend(StorageFileError):
    """Requested object path does not exist (or access denied)."""


# ---- Domain shapes ------------------------------------------------------------


@dataclass(frozen=True)
class FileRef:
    """Handle returned by ``StorageFileBackend.put``.

    The ``storage_ref`` is what we persist on ``referral_attachments``; the
    ``mime_type`` and ``size_bytes`` are informational and may be shown in
    the UI without a fresh download.
    """

    storage_ref: str
    mime_type: str
    size_bytes: int


def build_object_path(
    *,
    scope: Scope,
    referral_id: int,
    attachment_id: int,
    mime_type: str,
) -> str:
    """Compose ``{scope_prefix}/{referral_id}/{attachment_id}.{ext}``.

    Scope is denormalized into the first segment — ``org-{id}`` for org-
    mode callers, ``user-{id}`` for solo-mode callers.  Anonymous scope is
    rejected at the abstraction layer; callers must authenticate before
    uploading.  Object paths are not user-visible; they're opaque keys.
    """
    if scope.is_anonymous:
        raise StorageFileError("Cannot build object path for anonymous scope.")

    if scope.is_org:
        prefix = f"org-{scope.organization_id}"
    else:
        prefix = f"user-{scope.user_id}"

    suffix = MIME_TO_SUFFIX.get(mime_type, "bin")
    return f"{prefix}/{referral_id}/{attachment_id}.{suffix}"


# ---- Protocol -----------------------------------------------------------------


class StorageFileBackend(Protocol):
    """Minimal async-safe surface every backend implements."""

    async def put(
        self,
        *,
        path: str,
        data: bytes,
        mime_type: str,
    ) -> FileRef:
        """Write ``data`` at ``path``.  Overwrites silently.  Returns the
        ``FileRef`` the caller should persist on ``referral_attachments``.
        """

    async def get_bytes(self, path: str) -> bytes:
        """Fetch raw bytes.  Raises :class:`FileNotFoundInBackend` on miss."""

    async def signed_url(self, path: str, *, expires_in_seconds: int = 900) -> str:
        """Return a short-lived URL suitable for direct client download.

        Default 15 minute expiry matches the product doc; callers shorten
        for higher-sensitivity flows.
        """

    async def delete(self, path: str) -> None:
        """Best-effort delete.  Missing objects are treated as success."""
