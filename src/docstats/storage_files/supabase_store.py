"""Supabase Storage ``StorageFileBackend`` — Phase 10.A.

Production backend.  Requires ``SUPABASE_URL`` + ``SUPABASE_SERVICE_KEY``
(already in the stack for Postgres) plus ``SUPABASE_STORAGE_BUCKET``
naming the private bucket.  Bucket must be created ahead of time via the
Supabase dashboard or CLI — we don't auto-create (that would demand
owner-level creds and silently grant write access).

PHI posture
-----------
- Bucket is **private**.  Every download flows through a short-lived
  signed URL (default 15 min).
- Supabase provides AES-256 at rest on all Storage objects.
- Path layout denormalizes scope into the first segment so a leaked
  URL can't cross-tenant (see ``base.build_object_path``).
- Route-level audit rows (``action=attachment.view``) record who pulled
  each signed URL.

supabase-py quirks handled here:
  - ``upload`` raises on overwrite; we pass ``file_options={"upsert": "true"}``
    which maps to the ``x-upsert`` REST header.  Without it, retrying a
    failed upload against the same attachment id (same path) 409s.
  - ``remove`` tolerates missing paths and returns ``[]`` with no error.
  - ``download`` raises on missing path; we map to
    :class:`FileNotFoundInBackend`.
  - Storage calls are sync in the supabase-py 2.x client; we run them in
    ``run_in_executor`` so the event loop doesn't stall on 20 MB uploads.
"""

from __future__ import annotations

import asyncio
import logging
import os

from docstats.storage_files.base import (
    FileNotFoundInBackend,
    FileRef,
    StorageFileBackend,
    StorageFileError,
)

logger = logging.getLogger(__name__)


class SupabaseFileBackend(StorageFileBackend):
    def __init__(
        self,
        *,
        url: str | None = None,
        service_key: str | None = None,
        bucket: str | None = None,
    ) -> None:
        url = url or os.environ.get("SUPABASE_URL", "")
        service_key = service_key or os.environ.get("SUPABASE_SERVICE_KEY", "")
        bucket = bucket or os.environ.get("SUPABASE_STORAGE_BUCKET", "attachments")
        if not url or not service_key:
            raise StorageFileError(
                "SUPABASE_URL + SUPABASE_SERVICE_KEY must be set to use Supabase Storage"
            )
        # Lazy-import so unit tests that use the in-memory backend don't
        # pay the supabase import cost.
        from supabase import create_client

        self._bucket_name = bucket
        self._client = create_client(url, service_key)

    def _bucket(self):
        return self._client.storage.from_(self._bucket_name)

    async def put(self, *, path: str, data: bytes, mime_type: str) -> FileRef:
        def _sync() -> None:
            self._bucket().upload(
                path=path,
                file=data,
                file_options={"content-type": mime_type, "upsert": "true"},
            )

        try:
            await asyncio.get_running_loop().run_in_executor(None, _sync)
        except Exception as exc:  # storage3 raises its own hierarchy
            logger.exception("Supabase upload failed for %s", path)
            raise StorageFileError(f"Upload failed: {exc}") from exc
        return FileRef(storage_ref=path, mime_type=mime_type, size_bytes=len(data))

    async def get_bytes(self, path: str) -> bytes:
        def _sync() -> bytes:
            raw = self._bucket().download(path)
            # storage3 returns bytes; cast defensively for mypy.
            return bytes(raw)

        try:
            return await asyncio.get_running_loop().run_in_executor(None, _sync)
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "404" in msg:
                raise FileNotFoundInBackend(path) from exc
            logger.exception("Supabase download failed for %s", path)
            raise StorageFileError(f"Download failed: {exc}") from exc

    async def signed_url(self, path: str, *, expires_in_seconds: int = 900) -> str:
        def _sync() -> str:
            resp = self._bucket().create_signed_url(path, expires_in_seconds)
            # supabase-py returns a dict with ``signedURL`` (storage3 2.x) or
            # ``signedUrl`` depending on release; accept either.
            url = resp.get("signedURL") or resp.get("signedUrl") or ""
            if not url:
                raise StorageFileError(f"signed_url returned no URL: {resp!r}")
            return url

        try:
            return await asyncio.get_running_loop().run_in_executor(None, _sync)
        except StorageFileError:
            raise
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "404" in msg:
                raise FileNotFoundInBackend(path) from exc
            logger.exception("Supabase signed_url failed for %s", path)
            raise StorageFileError(f"Signed URL failed: {exc}") from exc

    async def delete(self, path: str) -> None:
        def _sync() -> None:
            self._bucket().remove([path])

        try:
            await asyncio.get_running_loop().run_in_executor(None, _sync)
        except Exception:
            # Delete is best-effort — surface as a log event rather than
            # failing the caller.  Orphans are swept by the retention job
            # (Phase 10.C).
            logger.exception("Supabase delete failed for %s", path)
