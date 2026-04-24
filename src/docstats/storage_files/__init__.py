"""Blob storage for attachment files — Phase 10.

The package is the single surface over which PDF / image / document uploads
travel on their way to Supabase Storage (production) or S3 (fallback,
Phase 10 follow-up).  The DB row (``referral_attachments.storage_ref``)
holds the opaque path used to look up the bytes; the bytes themselves
never flow through the database.

Design contract
---------------
- ``StorageFileBackend`` is a Protocol with 4 ops (``put``, ``get_bytes``,
  ``signed_url``, ``delete``).  Adapters implement it; callers are backend-
  agnostic.
- Scope is denormalized into the object path (``{org_id|user_id}/
  {referral_id}/{attachment_id}.{ext}``) so even a stolen bucket reference
  doesn't leak cross-org.
- MIME allow-list is enforced at the route layer via ``sniff_mime()``;
  the backend is byte-blind.
- Signed URLs default to 15-minute expiry — matches the product doc;
  callers can override for special-case flows.

Phase 10.A ships:
  - ``StorageFileBackend`` Protocol + helpers
  - ``SupabaseFileBackend`` (production)
  - ``InMemoryFileBackend`` (tests; also the dev fallback when Supabase
    credentials are absent)
  - MIME sniffing against a narrow allow-list

Virus scanning (10.B), retention (10.C), and packet embedding (10.D) drop
in on top of this surface.
"""

from __future__ import annotations

from docstats.storage_files.base import (
    ALLOWED_MIME_TYPES,
    MAX_UPLOAD_BYTES,
    FileNotFoundInBackend,
    FileRef,
    StorageFileBackend,
    StorageFileError,
    build_object_path,
)
from docstats.storage_files.factory import get_file_backend
from docstats.storage_files.memory_store import InMemoryFileBackend
from docstats.storage_files.mime import MimeSniffError, sniff_mime
from docstats.storage_files.scanner import ScannerUnavailable, ScanResult, VirusScanner
from docstats.storage_files.scanner_factory import (
    get_virus_scanner,
    virus_scan_is_required,
)

__all__ = [
    "ALLOWED_MIME_TYPES",
    "MAX_UPLOAD_BYTES",
    "FileNotFoundInBackend",
    "FileRef",
    "InMemoryFileBackend",
    "MimeSniffError",
    "ScanResult",
    "ScannerUnavailable",
    "StorageFileBackend",
    "StorageFileError",
    "VirusScanner",
    "build_object_path",
    "get_file_backend",
    "get_virus_scanner",
    "sniff_mime",
    "virus_scan_is_required",
]
