"""Persistence for saved providers and search history.

Supports two backends:
- SQLite (default): local development and CLI usage
- Supabase Postgres: production, when SUPABASE_URL + SUPABASE_SERVICE_KEY env vars are set
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from docstats.models import NPIResult, SavedProvider, SearchHistoryEntry

logger = logging.getLogger(__name__)

DEFAULT_DB_DIR = Path.home() / ".local" / "share" / "docstats"

_storage: "Storage | PostgresStorage | None" = None


def get_db_path(db_dir: Path | None = None) -> Path:
    """Return the database file path, creating the directory if needed."""
    d = db_dir or DEFAULT_DB_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / "docstats.db"


def _use_postgres() -> bool:
    """Return True if Supabase env vars are configured."""
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"))


def get_storage() -> "Storage | PostgresStorage":
    """Return the singleton storage instance (Postgres if configured, else SQLite)."""
    global _storage
    if _storage is None:
        if _use_postgres():
            _storage = PostgresStorage()
            logger.info("Using Supabase Postgres storage")
        else:
            _storage = Storage()
            logger.info("Using SQLite storage")
    return _storage


class Storage:
    """SQLite storage for saved providers and search history."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or get_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT,
                github_id     TEXT UNIQUE,
                github_login  TEXT,
                display_name  TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                last_login_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
            CREATE INDEX IF NOT EXISTS idx_users_github_id ON users(github_id);

            CREATE TABLE IF NOT EXISTS search_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                query_params TEXT NOT NULL,
                result_count INTEGER NOT NULL,
                user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
                searched_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_history_searched_at
                ON search_history(searched_at);
        """)
        self._conn.commit()
        self._migrate_saved_providers()
        self._migrate_search_history_user_id()
        self._migrate_users_pcp_npi()

    def _migrate_saved_providers(self) -> None:
        """Rebuild saved_providers with (user_id, npi) composite PK if needed."""
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(saved_providers)").fetchall()
        }
        if "user_id" in cols:
            return  # already migrated

        logger.info("Migrating saved_providers to per-user schema")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS saved_providers_new (
                user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                npi           TEXT NOT NULL,
                display_name  TEXT NOT NULL,
                entity_type   TEXT NOT NULL DEFAULT 'Individual',
                specialty     TEXT,
                phone         TEXT,
                fax           TEXT,
                address_line1 TEXT,
                address_city  TEXT,
                address_state TEXT,
                address_zip   TEXT,
                raw_json      TEXT NOT NULL,
                notes         TEXT,
                appt_address  TEXT,
                saved_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, npi)
            );
            CREATE INDEX IF NOT EXISTS idx_saved_providers_user
                ON saved_providers_new(user_id);
            DROP TABLE IF EXISTS saved_providers;
            ALTER TABLE saved_providers_new RENAME TO saved_providers;
        """)
        self._conn.commit()

    def _migrate_search_history_user_id(self) -> None:
        """Add user_id column to search_history if not present."""
        try:
            self._conn.execute(
                "ALTER TABLE search_history ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_user ON search_history(user_id)"
            )
            self._conn.commit()
        except Exception:
            pass  # Column already exists

    def _migrate_users_pcp_npi(self) -> None:
        """Add pcp_npi column to users if not present."""
        try:
            self._conn.execute("ALTER TABLE users ADD COLUMN pcp_npi TEXT")
            self._conn.commit()
        except Exception:
            pass  # Column already exists

    # --- User CRUD ---

    def create_user(self, email: str, password_hash: str) -> int:
        """Create a new email/password user. Returns the new user id."""
        cursor = self._conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            (email.strip().lower(), password_hash),
        )
        self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def get_user_by_id(self, user_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_email(self, email: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
        return dict(row) if row else None

    def get_user_by_github_id(self, github_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM users WHERE github_id = ?", (str(github_id),)
        ).fetchone()
        return dict(row) if row else None

    def upsert_github_user(
        self,
        github_id: str,
        github_login: str,
        email: str | None,
        display_name: str | None,
    ) -> int:
        """Insert or update a GitHub-authenticated user. Returns user id."""
        github_id = str(github_id)
        existing = self.get_user_by_github_id(github_id)
        if existing:
            self._conn.execute(
                "UPDATE users SET github_login=?, display_name=COALESCE(?, display_name),"
                " last_login_at=datetime('now') WHERE id=?",
                (github_login, display_name, existing["id"]),
            )
            self._conn.commit()
            return existing["id"]
        # Email may match an account created via email/password — link them
        if email:
            existing_email = self.get_user_by_email(email)
            if existing_email:
                self._conn.execute(
                    "UPDATE users SET github_id=?, github_login=?, last_login_at=datetime('now') WHERE id=?",
                    (github_id, github_login, existing_email["id"]),
                )
                self._conn.commit()
                return existing_email["id"]
        # Completely new user
        safe_email = email.strip().lower() if email else f"github_{github_id}@noemail.invalid"
        cursor = self._conn.execute(
            "INSERT INTO users (email, github_id, github_login, display_name) VALUES (?, ?, ?, ?)",
            (safe_email, github_id, github_login, display_name),
        )
        self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def update_last_login(self, user_id: int) -> None:
        self._conn.execute(
            "UPDATE users SET last_login_at=datetime('now') WHERE id=?", (user_id,)
        )
        self._conn.commit()

    def set_user_pcp(self, user_id: int, pcp_npi: str) -> None:
        self._conn.execute(
            "UPDATE users SET pcp_npi=? WHERE id=?", (pcp_npi, user_id)
        )
        self._conn.commit()

    def clear_user_pcp(self, user_id: int) -> None:
        self._conn.execute(
            "UPDATE users SET pcp_npi=NULL WHERE id=?", (user_id,)
        )
        self._conn.commit()

    # --- Provider CRUD ---

    def save_provider(
        self, result: NPIResult, user_id: int, notes: str | None = None
    ) -> SavedProvider:
        """Save or update a provider for a user."""
        provider = SavedProvider.from_npi_result(result, notes=notes)
        self._conn.execute(
            """
            INSERT INTO saved_providers
                (user_id, npi, display_name, entity_type, specialty, phone, fax,
                 address_line1, address_city, address_state, address_zip,
                 raw_json, notes, appt_address, saved_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, npi) DO UPDATE SET
                display_name=excluded.display_name,
                entity_type=excluded.entity_type,
                specialty=excluded.specialty,
                phone=excluded.phone,
                fax=excluded.fax,
                address_line1=excluded.address_line1,
                address_city=excluded.address_city,
                address_state=excluded.address_state,
                address_zip=excluded.address_zip,
                raw_json=excluded.raw_json,
                notes=COALESCE(excluded.notes, saved_providers.notes),
                updated_at=excluded.updated_at
            """,
            (
                user_id,
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
                None,  # appt_address: always NULL on initial save; preserved on conflict
                provider.saved_at.isoformat() if provider.saved_at else datetime.now().isoformat(),
                provider.updated_at.isoformat() if provider.updated_at else datetime.now().isoformat(),
            ),
        )
        self._conn.commit()
        logger.info("Saved provider %s: %s (user %s)", provider.npi, provider.display_name, user_id)
        return provider

    def get_provider(self, npi: str, user_id: int | None) -> SavedProvider | None:
        """Get a saved provider by NPI for a specific user. Returns None for anonymous."""
        if user_id is None:
            return None
        row = self._conn.execute(
            "SELECT * FROM saved_providers WHERE npi = ? AND user_id = ?", (npi, user_id)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_provider(row)

    def list_providers(self, user_id: int) -> list[SavedProvider]:
        """List all saved providers for a user, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM saved_providers WHERE user_id = ? ORDER BY saved_at DESC, npi DESC",
            (user_id,),
        ).fetchall()
        return [self._row_to_provider(r) for r in rows]

    def delete_provider(self, npi: str, user_id: int) -> bool:
        """Delete a saved provider. Returns True if it existed."""
        cursor = self._conn.execute(
            "DELETE FROM saved_providers WHERE npi = ? AND user_id = ?", (npi, user_id)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def set_appt_address(self, npi: str, address: str, user_id: int) -> bool:
        """Set the appointment address for a saved provider."""
        cursor = self._conn.execute(
            "UPDATE saved_providers SET appt_address = ? WHERE npi = ? AND user_id = ?",
            (address.strip(), npi, user_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def clear_appt_address(self, npi: str, user_id: int) -> bool:
        """Clear the appointment address for a saved provider."""
        cursor = self._conn.execute(
            "UPDATE saved_providers SET appt_address = NULL WHERE npi = ? AND user_id = ?",
            (npi, user_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def log_search(
        self, params: dict[str, str], result_count: int, user_id: int | None = None
    ) -> None:
        """Record a search in history."""
        self._conn.execute(
            "INSERT INTO search_history (query_params, result_count, user_id) VALUES (?, ?, ?)",
            (json.dumps(params), result_count, user_id),
        )
        self._conn.commit()

    def get_history(self, limit: int = 20, user_id: int | None = None) -> list[SearchHistoryEntry]:
        """Get recent search history for a user. Returns empty list for anonymous."""
        if user_id is None:
            return []
        rows = self._conn.execute(
            "SELECT * FROM search_history WHERE user_id = ?"
            " ORDER BY searched_at DESC, id DESC LIMIT ?",
            (user_id, limit),
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
            appt_address=row["appt_address"],
            saved_at=datetime.fromisoformat(row["saved_at"]) if row["saved_at"] else None,
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
        )


# ---------------------------------------------------------------------------
# Supabase Postgres backend
# ---------------------------------------------------------------------------


class PostgresStorage:
    """Supabase-backed storage using the supabase-py REST client.

    Tables are prefixed with ``docstats_`` to coexist with other apps in
    the same Supabase project.
    """

    def __init__(self) -> None:
        from supabase import create_client  # lazy import

        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_KEY"]
        self._client = create_client(url, key)
        self._zip_loaded = False

    # --- helpers ---

    def _t(self, name: str):
        """Return a table reference with the docstats_ prefix."""
        return self._client.table(f"docstats_{name}")

    @staticmethod
    def _parse_ts(value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value.replace("+00:00", "+00:00"))

    @staticmethod
    def _row_to_provider(row: dict) -> SavedProvider:
        def _ts(v):
            if not v:
                return None
            return datetime.fromisoformat(v)

        return SavedProvider(
            npi=row["npi"],
            display_name=row["display_name"],
            entity_type=row["entity_type"],
            specialty=row.get("specialty"),
            phone=row.get("phone"),
            fax=row.get("fax"),
            address_line1=row.get("address_line1"),
            address_city=row.get("address_city"),
            address_state=row.get("address_state"),
            address_zip=row.get("address_zip"),
            raw_json=row["raw_json"],
            notes=row.get("notes"),
            appt_address=row.get("appt_address"),
            saved_at=_ts(row.get("saved_at")),
            updated_at=_ts(row.get("updated_at")),
        )

    # --- User CRUD ---

    def create_user(self, email: str, password_hash: str) -> int:
        result = self._t("users").insert(
            {"email": email.strip().lower(), "password_hash": password_hash}
        ).execute()
        return result.data[0]["id"]

    def get_user_by_id(self, user_id: int) -> dict | None:
        result = self._t("users").select("*").eq("id", user_id).execute()
        return result.data[0] if result.data else None

    def get_user_by_email(self, email: str) -> dict | None:
        result = self._t("users").select("*").ilike("email", email.strip().lower()).execute()
        return result.data[0] if result.data else None

    def get_user_by_github_id(self, github_id: str) -> dict | None:
        result = self._t("users").select("*").eq("github_id", str(github_id)).execute()
        return result.data[0] if result.data else None

    def upsert_github_user(
        self,
        github_id: str,
        github_login: str,
        email: str | None,
        display_name: str | None,
    ) -> int:
        github_id = str(github_id)
        existing = self.get_user_by_github_id(github_id)
        if existing:
            self._t("users").update(
                {"github_login": github_login, "last_login_at": datetime.now().isoformat()}
            ).eq("id", existing["id"]).execute()
            return existing["id"]
        if email:
            existing_email = self.get_user_by_email(email)
            if existing_email:
                self._t("users").update(
                    {"github_id": github_id, "github_login": github_login, "last_login_at": datetime.now().isoformat()}
                ).eq("id", existing_email["id"]).execute()
                return existing_email["id"]
        safe_email = email.strip().lower() if email else f"github_{github_id}@noemail.invalid"
        result = self._t("users").insert(
            {"email": safe_email, "github_id": github_id, "github_login": github_login, "display_name": display_name}
        ).execute()
        return result.data[0]["id"]

    def update_last_login(self, user_id: int) -> None:
        self._t("users").update({"last_login_at": datetime.now().isoformat()}).eq("id", user_id).execute()

    def set_user_pcp(self, user_id: int, pcp_npi: str) -> None:
        self._t("users").update({"pcp_npi": pcp_npi}).eq("id", user_id).execute()

    def clear_user_pcp(self, user_id: int) -> None:
        self._t("users").update({"pcp_npi": None}).eq("id", user_id).execute()

    # --- Provider CRUD ---

    def save_provider(
        self, result: NPIResult, user_id: int, notes: str | None = None
    ) -> SavedProvider:
        provider = SavedProvider.from_npi_result(result, notes=notes)
        self._t("saved_providers").upsert(
            {
                "user_id": user_id,
                "npi": provider.npi,
                "display_name": provider.display_name,
                "entity_type": provider.entity_type,
                "specialty": provider.specialty,
                "phone": provider.phone,
                "fax": provider.fax,
                "address_line1": provider.address_line1,
                "address_city": provider.address_city,
                "address_state": provider.address_state,
                "address_zip": provider.address_zip,
                "raw_json": provider.raw_json,
                "notes": provider.notes,
                "saved_at": provider.saved_at.isoformat() if provider.saved_at else None,
                "updated_at": provider.updated_at.isoformat() if provider.updated_at else None,
            },
            on_conflict="user_id,npi",
        ).execute()
        logger.info("Saved provider %s: %s (user %s)", provider.npi, provider.display_name, user_id)
        return provider

    def get_provider(self, npi: str, user_id: int | None) -> SavedProvider | None:
        if user_id is None:
            return None
        result = (
            self._t("saved_providers")
            .select("*")
            .eq("npi", npi)
            .eq("user_id", user_id)
            .execute()
        )
        return self._row_to_provider(result.data[0]) if result.data else None

    def list_providers(self, user_id: int) -> list[SavedProvider]:
        result = (
            self._t("saved_providers")
            .select("*")
            .eq("user_id", user_id)
            .order("saved_at", desc=True)
            .order("npi", desc=True)
            .execute()
        )
        return [self._row_to_provider(r) for r in result.data]

    def delete_provider(self, npi: str, user_id: int) -> bool:
        result = (
            self._t("saved_providers")
            .delete()
            .eq("npi", npi)
            .eq("user_id", user_id)
            .execute()
        )
        return len(result.data) > 0

    def set_appt_address(self, npi: str, address: str, user_id: int) -> bool:
        result = (
            self._t("saved_providers")
            .update({"appt_address": address.strip()})
            .eq("npi", npi)
            .eq("user_id", user_id)
            .execute()
        )
        return len(result.data) > 0

    def clear_appt_address(self, npi: str, user_id: int) -> bool:
        result = (
            self._t("saved_providers")
            .update({"appt_address": None})
            .eq("npi", npi)
            .eq("user_id", user_id)
            .execute()
        )
        return len(result.data) > 0

    # --- Search history ---

    def log_search(
        self, params: dict[str, str], result_count: int, user_id: int | None = None
    ) -> None:
        self._t("search_history").insert(
            {"query_params": json.dumps(params), "result_count": result_count, "user_id": user_id}
        ).execute()

    def get_history(self, limit: int = 20, user_id: int | None = None) -> list[SearchHistoryEntry]:
        if user_id is None:
            return []
        result = (
            self._t("search_history")
            .select("*")
            .eq("user_id", user_id)
            .order("searched_at", desc=True)
            .order("id", desc=True)
            .limit(limit)
            .execute()
        )
        return [
            SearchHistoryEntry(
                id=r["id"],
                query_params=json.loads(r["query_params"]),
                result_count=r["result_count"],
                searched_at=datetime.fromisoformat(r["searched_at"]) if r.get("searched_at") else None,
            )
            for r in result.data
        ]

    # --- ZIP code lookup ---

    def lookup_zip(self, zip_code: str) -> dict[str, str] | None:
        self._ensure_zip_table()
        result = (
            self._t("zip_codes")
            .select("city,state")
            .eq("zip_code", zip_code.strip()[:5])
            .execute()
        )
        if not result.data:
            return None
        return {"city": result.data[0]["city"], "state": result.data[0]["state"]}

    def _ensure_zip_table(self) -> None:
        if self._zip_loaded:
            return
        # Check if table has data
        result = self._t("zip_codes").select("zip_code").limit(1).execute()
        if result.data:
            self._zip_loaded = True
            return
        # Load from JSON
        data_file = Path(__file__).parent / "data" / "zipcodes.json"
        if data_file.exists():
            data = json.loads(data_file.read_text())
            # Insert in batches of 500
            rows = [{"zip_code": z["zip"], "city": z["city"], "state": z["state"]} for z in data]
            for i in range(0, len(rows), 500):
                self._t("zip_codes").upsert(rows[i : i + 500], on_conflict="zip_code").execute()
            logger.info("Loaded %d ZIP codes into Supabase", len(data))
        else:
            logger.warning("ZIP code data file not found at %s", data_file)
        self._zip_loaded = True

    def close(self) -> None:
        pass  # supabase-py client has no close method
