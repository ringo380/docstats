"""SQLite persistence for saved providers and search history."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from docstats.models import NPIResult, SavedProvider, SearchHistoryEntry

logger = logging.getLogger(__name__)

DEFAULT_DB_DIR = Path.home() / ".local" / "share" / "docstats"


def get_db_path(db_dir: Path | None = None) -> Path:
    """Return the database file path, creating the directory if needed."""
    d = db_dir or DEFAULT_DB_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / "docstats.db"


class Storage:
    """SQLite storage for saved providers and search history."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or get_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS saved_providers (
                npi TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                entity_type TEXT NOT NULL DEFAULT 'Individual',
                specialty TEXT,
                phone TEXT,
                fax TEXT,
                address_line1 TEXT,
                address_city TEXT,
                address_state TEXT,
                address_zip TEXT,
                raw_json TEXT NOT NULL,
                notes TEXT,
                saved_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_params TEXT NOT NULL,
                result_count INTEGER NOT NULL,
                searched_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_history_searched_at
            ON search_history(searched_at);
        """)
        self._conn.commit()

    def save_provider(self, result: NPIResult, notes: str | None = None) -> SavedProvider:
        """Save or update a provider from an API result."""
        provider = SavedProvider.from_npi_result(result, notes=notes)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO saved_providers
                (npi, display_name, entity_type, specialty, phone, fax,
                 address_line1, address_city, address_state, address_zip,
                 raw_json, notes, saved_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider.npi,
                provider.display_name,
                provider.entity_type,
                provider.specialty,
                provider.phone,
                provider.fax,
                provider.address_line1,
                provider.address_city,
                provider.address_state,
                provider.address_zip,
                provider.raw_json,
                provider.notes,
                provider.saved_at.isoformat() if provider.saved_at else datetime.now().isoformat(),
                provider.updated_at.isoformat() if provider.updated_at else datetime.now().isoformat(),
            ),
        )
        self._conn.commit()
        logger.info("Saved provider %s: %s", provider.npi, provider.display_name)
        return provider

    def get_provider(self, npi: str) -> SavedProvider | None:
        """Get a saved provider by NPI."""
        row = self._conn.execute(
            "SELECT * FROM saved_providers WHERE npi = ?", (npi,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_provider(row)

    def list_providers(self) -> list[SavedProvider]:
        """List all saved providers, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM saved_providers ORDER BY saved_at DESC"
        ).fetchall()
        return [self._row_to_provider(r) for r in rows]

    def delete_provider(self, npi: str) -> bool:
        """Delete a saved provider. Returns True if it existed."""
        cursor = self._conn.execute(
            "DELETE FROM saved_providers WHERE npi = ?", (npi,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def log_search(self, params: dict[str, str], result_count: int) -> None:
        """Record a search in history."""
        self._conn.execute(
            "INSERT INTO search_history (query_params, result_count) VALUES (?, ?)",
            (json.dumps(params), result_count),
        )
        self._conn.commit()

    def get_history(self, limit: int = 20) -> list[SearchHistoryEntry]:
        """Get recent search history."""
        rows = self._conn.execute(
            "SELECT * FROM search_history ORDER BY searched_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            SearchHistoryEntry(
                id=r["id"],
                query_params=json.loads(r["query_params"]),
                result_count=r["result_count"],
                searched_at=datetime.fromisoformat(r["searched_at"]),
            )
            for r in rows
        ]

    # --- ZIP code lookup ---

    def lookup_zip(self, zip_code: str) -> dict[str, str] | None:
        """Look up city/state for a ZIP code. Lazy-loads ZIP data on first call."""
        self._ensure_zip_table()
        row = self._conn.execute(
            "SELECT city, state FROM zip_codes WHERE zip_code = ?",
            (zip_code.strip()[:5],),
        ).fetchone()
        if row is None:
            return None
        return {"city": row["city"], "state": row["state"]}

    def _ensure_zip_table(self) -> None:
        """Create and populate the zip_codes table if it doesn't exist."""
        exists = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='zip_codes'"
        ).fetchone()
        if exists:
            return

        self._conn.execute("""
            CREATE TABLE zip_codes (
                zip_code TEXT PRIMARY KEY,
                city TEXT NOT NULL,
                state TEXT NOT NULL
            )
        """)

        # Load from bundled JSON data
        data_file = Path(__file__).parent / "data" / "zipcodes.json"
        if data_file.exists():
            data = json.loads(data_file.read_text())
            self._conn.executemany(
                "INSERT OR IGNORE INTO zip_codes (zip_code, city, state) VALUES (?, ?, ?)",
                [(z["zip"], z["city"], z["state"]) for z in data],
            )
            self._conn.commit()
            logger.info("Loaded %d ZIP codes into database", len(data))
        else:
            logger.warning("ZIP code data file not found at %s", data_file)
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_provider(row: sqlite3.Row) -> SavedProvider:
        return SavedProvider(
            npi=row["npi"],
            display_name=row["display_name"],
            entity_type=row["entity_type"],
            specialty=row["specialty"],
            phone=row["phone"],
            fax=row["fax"],
            address_line1=row["address_line1"],
            address_city=row["address_city"],
            address_state=row["address_state"],
            address_zip=row["address_zip"],
            raw_json=row["raw_json"],
            notes=row["notes"],
            saved_at=datetime.fromisoformat(row["saved_at"]) if row["saved_at"] else None,
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
        )
