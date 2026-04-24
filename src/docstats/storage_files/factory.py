"""Factory for the active ``StorageFileBackend`` — Phase 10.A.

Single choke-point so the route layer never imports a concrete backend
directly.  Selection rule:

  - ``ATTACHMENT_STORAGE_BACKEND=supabase`` OR both Supabase env vars set
    (``SUPABASE_URL`` + ``SUPABASE_SERVICE_KEY``) → :class:`SupabaseFileBackend`.
  - ``ATTACHMENT_STORAGE_BACKEND=memory`` OR no Supabase creds → shared
    module-level :class:`InMemoryFileBackend` singleton.  The singleton is
    important: routes that upload and later download the same file need to
    see the same store.

Tests override via ``app.dependency_overrides[get_file_backend]``.
"""

from __future__ import annotations

import os

from docstats.storage_files.base import StorageFileBackend
from docstats.storage_files.memory_store import InMemoryFileBackend

_memory_singleton: InMemoryFileBackend | None = None


def _get_memory_singleton() -> InMemoryFileBackend:
    global _memory_singleton
    if _memory_singleton is None:
        _memory_singleton = InMemoryFileBackend()
    return _memory_singleton


def get_file_backend() -> StorageFileBackend:
    """Return the active backend.  FastAPI dep-injection target.

    Env-driven so tests can flip backends without code changes:

    - ``ATTACHMENT_STORAGE_BACKEND=memory`` → in-memory (default when
      Supabase is unconfigured).
    - ``ATTACHMENT_STORAGE_BACKEND=supabase`` → Supabase (requires creds).
    - unset → Supabase if creds present, else in-memory.
    """
    explicit = os.environ.get("ATTACHMENT_STORAGE_BACKEND", "").strip().lower()
    if explicit == "memory":
        return _get_memory_singleton()

    has_supabase_creds = bool(
        os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY")
    )

    if explicit == "supabase" or (not explicit and has_supabase_creds):
        from docstats.storage_files.supabase_store import SupabaseFileBackend

        return SupabaseFileBackend()

    return _get_memory_singleton()


def reset_memory_singleton_for_tests() -> None:
    """Test-only: drop the in-memory singleton so each test starts fresh."""
    global _memory_singleton
    _memory_singleton = None
