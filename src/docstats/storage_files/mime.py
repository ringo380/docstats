"""MIME sniffing — Phase 10.A.

Magic-byte detection over the narrow allow-list from ``base.ALLOWED_MIME_TYPES``.
We never trust the client-supplied ``Content-Type`` — it's easy to forge
and real uploads routinely mis-label (browsers tag ``.jpg`` as
``image/pjpeg`` or ``application/octet-stream``).  Sniffing from the
first bytes is authoritative.

Pure-Python on purpose — `python-magic` requires ``libmagic`` on the OS
which Railway's railpack builder doesn't include by default.  Adding it
means adding another ``buildAptPackages`` entry; avoided until a real
need surfaces.

DOCX detection
--------------
DOCX / XLSX / PPTX all share the ZIP magic (``PK\\x03\\x04``).  To narrow
we look for ``word/`` inside the central directory, which ships in every
DOCX but not XLSX/PPTX.  The check is a substring scan over the first
8 KB — good enough because DOCX puts ``[Content_Types].xml`` plus
``word/document.xml`` near the head of the archive.
"""

from __future__ import annotations

from typing import Final

_MAGIC_BYTES: Final[list[tuple[bytes, str]]] = [
    (b"%PDF-", "application/pdf"),
    # JPEG: both JFIF + EXIF headers start with FFD8FF.
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    # TIFF little-endian + big-endian.
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
]

_ZIP_MAGIC: Final[bytes] = b"PK\x03\x04"
_DOCX_MIME: Final[str] = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_DOCX_MARKER: Final[bytes] = b"word/"


class MimeSniffError(ValueError):
    """Bytes didn't match any allow-listed MIME type."""


def sniff_mime(data: bytes) -> str:
    """Return the MIME type of ``data``; raise :class:`MimeSniffError` on miss.

    Only returns MIME types present in
    :data:`docstats.storage_files.base.ALLOWED_MIME_TYPES`.  Ambiguous or
    unrecognized bytes raise; there is no "unknown" or "octet-stream"
    fallback.
    """
    if not data:
        raise MimeSniffError("Empty payload.")

    for magic, mime in _MAGIC_BYTES:
        if data.startswith(magic):
            return mime

    # ZIP family — narrow to DOCX by scanning the first 8 KB for the
    # ``word/`` path marker.
    if data.startswith(_ZIP_MAGIC):
        head = data[: 8 * 1024]
        if _DOCX_MARKER in head:
            return _DOCX_MIME
        raise MimeSniffError("ZIP archive detected but not a recognized Office document.")

    raise MimeSniffError("Unrecognized file type.")
