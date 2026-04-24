"""In-memory ``StorageFileBackend`` — Phase 10.A.

Used by the test suite (no network, no disk) and as the dev fallback when
``SUPABASE_URL`` / ``SUPABASE_SERVICE_KEY`` / ``SUPABASE_STORAGE_BUCKET``
aren't set.  Bytes are kept in a module-level dict keyed by path; signed
URLs are stub paths of the form ``inmemory://<path>`` so UI code can
render the link without special-casing the backend.
"""

from __future__ import annotations

import threading

from docstats.storage_files.base import (
    FileNotFoundInBackend,
    FileRef,
    StorageFileBackend,
)


class InMemoryFileBackend(StorageFileBackend):
    def __init__(self) -> None:
        self._store: dict[str, tuple[bytes, str]] = {}
        self._lock = threading.Lock()

    async def put(self, *, path: str, data: bytes, mime_type: str) -> FileRef:
        with self._lock:
            self._store[path] = (data, mime_type)
        return FileRef(storage_ref=path, mime_type=mime_type, size_bytes=len(data))

    async def get_bytes(self, path: str) -> bytes:
        with self._lock:
            entry = self._store.get(path)
        if entry is None:
            raise FileNotFoundInBackend(path)
        return entry[0]

    async def signed_url(self, path: str, *, expires_in_seconds: int = 900) -> str:
        # Stub — real backends return an https URL.  Callers that need to
        # dereference this in tests should call ``get_bytes`` instead.
        return f"inmemory://{path}"

    async def delete(self, path: str) -> None:
        with self._lock:
            self._store.pop(path, None)

    # Test helpers — not part of the Protocol.
    def _size(self) -> int:
        with self._lock:
            return len(self._store)

    def _has(self, path: str) -> bool:
        with self._lock:
            return path in self._store
