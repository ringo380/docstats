"""SQLite-based response cache with TTL expiry."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path

from docstats.models import NPIResponse

logger = logging.getLogger(__name__)

DEFAULT_TTL = 86400  # 24 hours


class ResponseCache:
    """Cache NPPES API responses in SQLite with time-based expiry."""

    def __init__(self, db_path: Path, ttl_seconds: int = DEFAULT_TTL) -> None:
        self._db_path = db_path
        self._ttl = ttl_seconds
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._init_table()

    def _init_table(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS response_cache (
                cache_key TEXT PRIMARY KEY,
                response_json TEXT NOT NULL,
                cached_at TEXT NOT NULL DEFAULT (datetime('now')),
                ttl_seconds INTEGER NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_cache_cached_at
            ON response_cache(cached_at)
        """)
        self._conn.commit()

    @staticmethod
    def _make_key(params: dict[str, str]) -> str:
        """Create a deterministic cache key from query parameters."""
        normalized = sorted((k.lower(), v.lower()) for k, v in params.items())
        raw = json.dumps(normalized, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, params: dict[str, str]) -> NPIResponse | None:
        """Retrieve a cached response if it exists and hasn't expired."""
        self._evict_expired()
        key = self._make_key(params)
        row = self._conn.execute(
            "SELECT response_json FROM response_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()

        if row is None:
            return None

        try:
            return NPIResponse.model_validate_json(row[0])
        except Exception:
            logger.warning("Failed to deserialize cached response, removing entry")
            self._conn.execute("DELETE FROM response_cache WHERE cache_key = ?", (key,))
            self._conn.commit()
            return None

    def set(self, params: dict[str, str], response: NPIResponse) -> None:
        """Store a response in the cache."""
        key = self._make_key(params)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO response_cache (cache_key, response_json, cached_at, ttl_seconds)
            VALUES (?, ?, datetime('now'), ?)
            """,
            (key, response.model_dump_json(), self._ttl),
        )
        self._conn.commit()

    def _evict_expired(self) -> None:
        """Remove expired cache entries."""
        self._conn.execute("""
            DELETE FROM response_cache
            WHERE datetime(cached_at, '+' || ttl_seconds || ' seconds') < datetime('now')
        """)
        self._conn.commit()

    def clear(self) -> None:
        """Remove all cached entries."""
        self._conn.execute("DELETE FROM response_cache")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
