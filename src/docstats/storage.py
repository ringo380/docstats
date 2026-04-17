"""Persistence for saved providers and search history.

Supports two backends:
- SQLite (default): local development and CLI usage
- Supabase Postgres: production, when SUPABASE_URL + SUPABASE_SERVICE_KEY env vars are set
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from docstats.domain.audit import AuditEvent
from docstats.domain.orgs import ROLES, Membership, Organization
from docstats.domain.patients import Patient
from docstats.domain.sessions import Session
from docstats.models import NPIResult, SavedProvider, SearchHistoryEntry
from docstats.scope import Scope, scope_sql_clause
from docstats.storage_base import StorageBase, fuzzy_score, normalize_email
from docstats.validators import IP_MAX_LENGTH, USER_AGENT_MAX_LENGTH

if TYPE_CHECKING:
    from docstats.pg_storage import PostgresStorage

logger = logging.getLogger(__name__)

DEFAULT_DB_DIR = Path.home() / ".local" / "share" / "docstats"


def _escape_like(query: str) -> str:
    """Escape SQL LIKE wildcard characters in a search query."""
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_sqlite_utc(value: str | None) -> datetime | None:
    """Parse a SQLite TEXT timestamp as tz-aware UTC.

    SQLite's ``datetime('now')`` returns naive UTC; attaching ``tz=timezone.utc``
    makes comparisons with ``datetime.now(tz=timezone.utc)`` consistent across
    backends. All ``_row_to_*`` helpers go through this so SQLite-sourced
    datetimes match the tz-aware datetimes Supabase returns.
    """
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_organization(row: sqlite3.Row) -> Organization:
    """Convert a SQLite organizations row into an Organization model."""
    created = _parse_sqlite_utc(row["created_at"])
    assert created is not None
    return Organization(
        id=int(row["id"]),
        name=row["name"],
        slug=row["slug"],
        npi=row["npi"],
        address_line1=row["address_line1"],
        address_line2=row["address_line2"],
        address_city=row["address_city"],
        address_state=row["address_state"],
        address_zip=row["address_zip"],
        phone=row["phone"],
        fax=row["fax"],
        terms_bundle_version=row["terms_bundle_version"],
        created_at=created,
        deleted_at=_parse_sqlite_utc(row["deleted_at"]),
    )


def _row_to_membership(row: sqlite3.Row) -> Membership:
    """Convert a SQLite memberships row into a Membership model."""
    joined = _parse_sqlite_utc(row["joined_at"])
    assert joined is not None
    return Membership(
        id=int(row["id"]),
        organization_id=int(row["organization_id"]),
        user_id=int(row["user_id"]),
        role=row["role"],
        invited_by_user_id=row["invited_by_user_id"],
        joined_at=joined,
        deleted_at=_parse_sqlite_utc(row["deleted_at"]),
    )


def _row_to_patient(row: sqlite3.Row) -> Patient:
    """Convert a SQLite patients row into a Patient model."""
    created = _parse_sqlite_utc(row["created_at"])
    updated = _parse_sqlite_utc(row["updated_at"])
    assert created is not None and updated is not None
    return Patient(
        id=int(row["id"]),
        scope_user_id=row["scope_user_id"],
        scope_organization_id=row["scope_organization_id"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        middle_name=row["middle_name"],
        date_of_birth=row["date_of_birth"],
        sex=row["sex"],
        mrn=row["mrn"],
        preferred_language=row["preferred_language"],
        pronouns=row["pronouns"],
        phone=row["phone"],
        email=row["email"],
        address_line1=row["address_line1"],
        address_line2=row["address_line2"],
        address_city=row["address_city"],
        address_state=row["address_state"],
        address_zip=row["address_zip"],
        emergency_contact_name=row["emergency_contact_name"],
        emergency_contact_phone=row["emergency_contact_phone"],
        notes=row["notes"],
        created_by_user_id=row["created_by_user_id"],
        created_at=created,
        updated_at=updated,
        deleted_at=_parse_sqlite_utc(row["deleted_at"]),
    )


def _row_to_session(row: sqlite3.Row) -> Session:
    """Convert a SQLite sessions row into a Session model."""
    data_raw = row["data_json"]
    created = _parse_sqlite_utc(row["created_at"])
    last_seen = _parse_sqlite_utc(row["last_seen_at"])
    expires = _parse_sqlite_utc(row["expires_at"])
    assert created is not None and last_seen is not None and expires is not None
    return Session(
        id=row["id"],
        user_id=row["user_id"],
        data=json.loads(data_raw) if data_raw else {},
        ip=row["ip"],
        user_agent=row["user_agent"],
        created_at=created,
        last_seen_at=last_seen,
        expires_at=expires,
        revoked_at=_parse_sqlite_utc(row["revoked_at"]),
    )


def _row_to_audit_event(row: sqlite3.Row) -> AuditEvent:
    """Convert a SQLite audit_events row into an AuditEvent."""
    metadata_raw = row["metadata_json"]
    created = _parse_sqlite_utc(row["created_at"])
    assert created is not None
    return AuditEvent(
        id=int(row["id"]),
        actor_user_id=row["actor_user_id"],
        scope_user_id=row["scope_user_id"],
        scope_organization_id=row["scope_organization_id"],
        action=row["action"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        metadata=json.loads(metadata_raw) if metadata_raw else {},
        ip=row["ip"],
        user_agent=row["user_agent"],
        created_at=created,
    )


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
            from docstats.pg_storage import PostgresStorage

            _storage = PostgresStorage()
            logger.info("Using Supabase Postgres storage")
        else:
            _storage = Storage()
            logger.info("Using SQLite storage")
    return _storage


class Storage(StorageBase):
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
        self._migrate_users_profile_fields()
        self._migrate_enrichment_json()
        self._migrate_appt_suite()
        self._migrate_is_televisit()
        self._migrate_appt_phone_fax()
        self._migrate_audit_events()
        self._migrate_orgs_and_memberships()
        self._migrate_users_active_org_and_role_hint()
        self._migrate_sessions()
        self._migrate_patients()

    def _migrate_saved_providers(self) -> None:
        """Rebuild saved_providers with (user_id, npi) composite PK if needed."""
        cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(saved_providers)").fetchall()
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
        except sqlite3.OperationalError:
            pass  # Column already exists

    def _migrate_users_pcp_npi(self) -> None:
        """Add pcp_npi column to users if not present."""
        try:
            self._conn.execute("ALTER TABLE users ADD COLUMN pcp_npi TEXT")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

    def _migrate_users_profile_fields(self) -> None:
        """Add profile and terms-acceptance columns to users if not present."""
        cols = [
            "first_name TEXT",
            "last_name TEXT",
            "middle_name TEXT",
            "date_of_birth TEXT",
            "terms_accepted_at TEXT",
            "terms_version TEXT",
            "terms_ip TEXT",
            "terms_user_agent TEXT",
        ]
        for col in cols:
            try:
                self._conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # Column already exists
        self._conn.commit()

    def _migrate_enrichment_json(self) -> None:
        """Add enrichment_json column to saved_providers if not present."""
        try:
            self._conn.execute("ALTER TABLE saved_providers ADD COLUMN enrichment_json TEXT")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

    def _migrate_appt_suite(self) -> None:
        """Add appt_suite column to saved_providers if not present."""
        try:
            self._conn.execute("ALTER TABLE saved_providers ADD COLUMN appt_suite TEXT")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

    def _migrate_is_televisit(self) -> None:
        """Add is_televisit column to saved_providers if not present."""
        try:
            self._conn.execute(
                "ALTER TABLE saved_providers ADD COLUMN is_televisit INTEGER DEFAULT 0"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

    def _migrate_appt_phone_fax(self) -> None:
        """Add appt_phone and appt_fax columns to saved_providers if not present."""
        for col in ("appt_phone", "appt_fax"):
            try:
                self._conn.execute(f"ALTER TABLE saved_providers ADD COLUMN {col} TEXT")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists

    def _migrate_audit_events(self) -> None:
        """Create the append-only audit_events table if absent."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS audit_events (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
                scope_user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
                scope_organization_id INTEGER,
                action                TEXT NOT NULL,
                entity_type           TEXT,
                entity_id             TEXT,
                metadata_json         TEXT,
                ip                    TEXT,
                user_agent            TEXT,
                created_at            TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_audit_events_actor
                ON audit_events(actor_user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_events_scope_user
                ON audit_events(scope_user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_events_scope_org
                ON audit_events(scope_organization_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_events_entity
                ON audit_events(entity_type, entity_id, created_at DESC);
        """)
        self._conn.commit()

    def _migrate_orgs_and_memberships(self) -> None:
        """Create organizations + memberships tables if absent."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS organizations (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                name                  TEXT NOT NULL,
                slug                  TEXT NOT NULL,
                npi                   TEXT,
                address_line1         TEXT,
                address_line2         TEXT,
                address_city          TEXT,
                address_state         TEXT,
                address_zip           TEXT,
                phone                 TEXT,
                fax                   TEXT,
                terms_bundle_version  TEXT,
                created_at            TEXT NOT NULL DEFAULT (datetime('now')),
                deleted_at            TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_organizations_live_slug
                ON organizations(slug) WHERE deleted_at IS NULL;

            CREATE TABLE IF NOT EXISTS memberships (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id     INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role                TEXT NOT NULL CHECK (role IN ('owner','admin','coordinator','clinician','staff','read_only')),
                invited_by_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
                joined_at           TEXT NOT NULL DEFAULT (datetime('now')),
                deleted_at          TEXT,
                UNIQUE(organization_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_memberships_user
                ON memberships(user_id) WHERE deleted_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_memberships_org
                ON memberships(organization_id) WHERE deleted_at IS NULL;
        """)
        self._conn.commit()

    def _migrate_users_active_org_and_role_hint(self) -> None:
        """Add active_org_id, role_hint, and PHI-consent columns to users."""
        for col in (
            "active_org_id INTEGER REFERENCES organizations(id) ON DELETE SET NULL",
            "role_hint TEXT",
            "phi_consent_version TEXT",
            "phi_consent_at TEXT",
            "phi_consent_ip TEXT",
            "phi_consent_user_agent TEXT",
        ):
            try:
                self._conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # Column already exists
        self._conn.commit()

    def _migrate_sessions(self) -> None:
        """Create the server-side sessions table if absent."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id            TEXT PRIMARY KEY,
                user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE,
                data_json     TEXT,
                ip            TEXT,
                user_agent    TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                last_seen_at  TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at    TEXT NOT NULL,
                revoked_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user
                ON sessions(user_id) WHERE revoked_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_sessions_expires
                ON sessions(expires_at) WHERE revoked_at IS NULL;
        """)
        self._conn.commit()

    def _migrate_patients(self) -> None:
        """Create the patients table if absent (Phase 1.A)."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS patients (
                id                        INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_user_id             INTEGER REFERENCES users(id) ON DELETE CASCADE,
                scope_organization_id     INTEGER REFERENCES organizations(id) ON DELETE CASCADE,
                first_name                TEXT NOT NULL,
                last_name                 TEXT NOT NULL,
                middle_name               TEXT,
                date_of_birth             TEXT,
                sex                       TEXT,
                mrn                       TEXT,
                preferred_language        TEXT,
                pronouns                  TEXT,
                phone                     TEXT,
                email                     TEXT,
                address_line1             TEXT,
                address_line2             TEXT,
                address_city              TEXT,
                address_state             TEXT,
                address_zip               TEXT,
                emergency_contact_name    TEXT,
                emergency_contact_phone   TEXT,
                notes                     TEXT,
                created_by_user_id        INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at                TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at                TEXT NOT NULL DEFAULT (datetime('now')),
                deleted_at                TEXT,
                CHECK (
                    (scope_user_id IS NOT NULL AND scope_organization_id IS NULL)
                    OR (scope_user_id IS NULL AND scope_organization_id IS NOT NULL)
                )
            );
            CREATE INDEX IF NOT EXISTS idx_patients_scope_user_name
                ON patients(scope_user_id, last_name, first_name) WHERE deleted_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_patients_scope_org_name
                ON patients(scope_organization_id, last_name, first_name) WHERE deleted_at IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_patients_org_mrn
                ON patients(scope_organization_id, mrn)
                WHERE scope_organization_id IS NOT NULL
                  AND mrn IS NOT NULL
                  AND deleted_at IS NULL;
        """)
        self._conn.commit()

    # --- User CRUD ---

    def create_user(self, email: str, password_hash: str) -> int:
        """Create a new email/password user. Returns the new user id."""
        cursor = self._conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            (normalize_email(email), password_hash),
        )
        self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def get_user_by_id(self, user_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_email(self, email: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM users WHERE email = ?", (normalize_email(email),)
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
            return int(existing["id"])
        # Email may match an account created via email/password — link them
        if email:
            existing_email = self.get_user_by_email(email)
            if existing_email:
                self._conn.execute(
                    "UPDATE users SET github_id=?, github_login=?, last_login_at=datetime('now') WHERE id=?",
                    (github_id, github_login, existing_email["id"]),
                )
                self._conn.commit()
                return int(existing_email["id"])
        # Completely new user
        safe_email = normalize_email(email) if email else f"github_{github_id}@noemail.invalid"
        cursor = self._conn.execute(
            "INSERT INTO users (email, github_id, github_login, display_name) VALUES (?, ?, ?, ?)",
            (safe_email, github_id, github_login, display_name),
        )
        self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def update_last_login(self, user_id: int) -> None:
        self._conn.execute("UPDATE users SET last_login_at=datetime('now') WHERE id=?", (user_id,))
        self._conn.commit()

    def set_user_pcp(self, user_id: int, pcp_npi: str) -> None:
        self._conn.execute("UPDATE users SET pcp_npi=? WHERE id=?", (pcp_npi, user_id))
        self._conn.commit()

    def clear_user_pcp(self, user_id: int) -> None:
        self._conn.execute("UPDATE users SET pcp_npi=NULL WHERE id=?", (user_id,))
        self._conn.commit()

    def update_user_profile(
        self,
        user_id: int,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        middle_name: str | None = None,
        date_of_birth: str | None = None,
        display_name: str | None = None,
    ) -> None:
        fields: dict[str, str] = {}
        if first_name is not None:
            fields["first_name"] = first_name
        if last_name is not None:
            fields["last_name"] = last_name
        if middle_name is not None:
            fields["middle_name"] = middle_name
        if date_of_birth is not None:
            fields["date_of_birth"] = date_of_birth
        if display_name is not None:
            fields["display_name"] = display_name
        if not fields:
            return
        set_clause = ", ".join(f"{k}=?" for k in fields)
        self._conn.execute(
            f"UPDATE users SET {set_clause} WHERE id=?",
            (*fields.values(), user_id),
        )
        self._conn.commit()

    def record_terms_acceptance(
        self,
        user_id: int,
        *,
        terms_version: str,
        ip_address: str,
        user_agent: str,
    ) -> None:
        self._conn.execute(
            "UPDATE users SET terms_accepted_at=datetime('now'), terms_version=?, terms_ip=?, terms_user_agent=? WHERE id=?",
            (terms_version, ip_address, user_agent, user_id),
        )
        self._conn.commit()

    def record_phi_consent(
        self,
        user_id: int,
        *,
        phi_consent_version: str,
        ip_address: str,
        user_agent: str,
    ) -> None:
        # Cap at storage boundary so callers can't blow up the users row with
        # oversized headers. Matches the pattern in domain/audit.py record().
        self._conn.execute(
            "UPDATE users SET phi_consent_at=datetime('now'), phi_consent_version=?, "
            "phi_consent_ip=?, phi_consent_user_agent=? WHERE id=?",
            (
                phi_consent_version,
                ip_address[:IP_MAX_LENGTH] if ip_address else ip_address,
                user_agent[:USER_AGENT_MAX_LENGTH] if user_agent else user_agent,
                user_id,
            ),
        )
        self._conn.commit()

    def set_active_org(self, user_id: int, organization_id: int | None) -> None:
        self._conn.execute(
            "UPDATE users SET active_org_id = ? WHERE id = ?",
            (organization_id, user_id),
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
                 raw_json, notes, appt_address, appt_suite, appt_phone, appt_fax,
                 is_televisit, saved_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                None,  # appt_suite: always NULL on initial save; preserved on conflict
                None,  # appt_phone: always NULL on initial save; preserved on conflict
                None,  # appt_fax: always NULL on initial save; preserved on conflict
                0,  # is_televisit: always 0 on initial save; preserved on conflict
                provider.saved_at.isoformat() if provider.saved_at else datetime.now().isoformat(),
                provider.updated_at.isoformat()
                if provider.updated_at
                else datetime.now().isoformat(),
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

    def search_providers(self, user_id: int, query: str) -> list[SavedProvider]:
        """Search saved providers by fuzzy matching against name, NPI, specialty, notes, and city."""
        pattern = f"%{_escape_like(query)}%"
        rows = self._conn.execute(
            """SELECT * FROM saved_providers
               WHERE user_id = ?
                 AND (display_name LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR npi LIKE ? ESCAPE '\\'
                   OR specialty LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR notes LIKE ? ESCAPE '\\' COLLATE NOCASE
                   OR address_city LIKE ? ESCAPE '\\' COLLATE NOCASE)""",
            (user_id, pattern, pattern, pattern, pattern, pattern),
        ).fetchall()
        providers = [self._row_to_provider(r) for r in rows]
        query_lower = query.lower()
        return sorted(providers, key=lambda p: fuzzy_score(p, query_lower), reverse=True)

    def delete_provider(self, npi: str, user_id: int) -> bool:
        """Delete a saved provider. Returns True if it existed."""
        cursor = self._conn.execute(
            "DELETE FROM saved_providers WHERE npi = ? AND user_id = ?", (npi, user_id)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def update_notes(self, npi: str, notes: str | None, user_id: int) -> bool:
        """Update notes for a saved provider. Returns True if it existed."""
        cursor = self._conn.execute(
            "UPDATE saved_providers SET notes = ?, updated_at = ? WHERE npi = ? AND user_id = ?",
            (notes, datetime.now().isoformat(), npi, user_id),
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

    def set_appt_suite(self, npi: str, suite: str | None, user_id: int) -> bool:
        """Set or clear the appointment suite/room for a saved provider."""
        cursor = self._conn.execute(
            "UPDATE saved_providers SET appt_suite = ? WHERE npi = ? AND user_id = ?",
            (suite.strip() if suite else None, npi, user_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def update_enrichment(self, npi: str, enrichment_json: str, user_id: int) -> bool:
        """Update enrichment data for a saved provider."""
        cursor = self._conn.execute(
            "UPDATE saved_providers SET enrichment_json = ?, updated_at = ? WHERE npi = ? AND user_id = ?",
            (enrichment_json, datetime.now().isoformat(), npi, user_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def clear_appt_address(self, npi: str, user_id: int) -> bool:
        """Clear the appointment address, suite, phone, and fax for a saved provider."""
        cursor = self._conn.execute(
            "UPDATE saved_providers SET appt_address = NULL, appt_suite = NULL, appt_phone = NULL, appt_fax = NULL WHERE npi = ? AND user_id = ?",
            (npi, user_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def set_televisit(self, npi: str, is_televisit: bool, user_id: int) -> bool:
        """Set or clear the televisit flag for a saved provider."""
        cursor = self._conn.execute(
            "UPDATE saved_providers SET is_televisit = ? WHERE npi = ? AND user_id = ?",
            (int(is_televisit), npi, user_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def set_appt_contact(self, npi: str, phone: str | None, fax: str | None, user_id: int) -> bool:
        """Set or clear the appointment phone and fax for a saved provider."""
        cursor = self._conn.execute(
            "UPDATE saved_providers SET appt_phone = ?, appt_fax = ? WHERE npi = ? AND user_id = ?",
            (phone.strip() if phone else None, fax.strip() if fax else None, npi, user_id),
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

    # --- Audit log (append-only) ---

    def record_audit_event(
        self,
        *,
        action: str,
        actor_user_id: int | None = None,
        scope_user_id: int | None = None,
        scope_organization_id: int | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> int:
        cursor = self._conn.execute(
            """INSERT INTO audit_events
               (actor_user_id, scope_user_id, scope_organization_id, action,
                entity_type, entity_id, metadata_json, ip, user_agent)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                actor_user_id,
                scope_user_id,
                scope_organization_id,
                action,
                entity_type,
                entity_id,
                json.dumps(metadata) if metadata else None,
                ip,
                user_agent,
            ),
        )
        self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def list_audit_events(
        self,
        *,
        actor_user_id: int | None = None,
        scope_user_id: int | None = None,
        scope_organization_id: int | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        clauses: list[str] = []
        params: list[Any] = []
        if actor_user_id is not None:
            clauses.append("actor_user_id = ?")
            params.append(actor_user_id)
        if scope_user_id is not None:
            clauses.append("scope_user_id = ?")
            params.append(scope_user_id)
        if scope_organization_id is not None:
            clauses.append("scope_organization_id = ?")
            params.append(scope_organization_id)
        if entity_type is not None:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        # Tiebreaker on id DESC so same-second rows stay deterministic.
        sql = f"SELECT * FROM audit_events{where} ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_audit_event(row) for row in rows]

    # --- Organizations & memberships ---

    def create_organization(
        self,
        *,
        name: str,
        slug: str,
        npi: str | None = None,
        address_line1: str | None = None,
        address_line2: str | None = None,
        address_city: str | None = None,
        address_state: str | None = None,
        address_zip: str | None = None,
        phone: str | None = None,
        fax: str | None = None,
        terms_bundle_version: str | None = None,
    ) -> Organization:
        cursor = self._conn.execute(
            """INSERT INTO organizations
               (name, slug, npi, address_line1, address_line2, address_city,
                address_state, address_zip, phone, fax, terms_bundle_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name,
                slug,
                npi,
                address_line1,
                address_line2,
                address_city,
                address_state,
                address_zip,
                phone,
                fax,
                terms_bundle_version,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM organizations WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_organization(row)

    def get_organization(self, organization_id: int) -> Organization | None:
        row = self._conn.execute(
            "SELECT * FROM organizations WHERE id = ? AND deleted_at IS NULL",
            (organization_id,),
        ).fetchone()
        return _row_to_organization(row) if row else None

    def get_organization_by_slug(self, slug: str) -> Organization | None:
        row = self._conn.execute(
            "SELECT * FROM organizations WHERE slug = ? AND deleted_at IS NULL",
            (slug,),
        ).fetchone()
        return _row_to_organization(row) if row else None

    def soft_delete_organization(self, organization_id: int) -> bool:
        cursor = self._conn.execute(
            "UPDATE organizations SET deleted_at = datetime('now') "
            "WHERE id = ? AND deleted_at IS NULL",
            (organization_id,),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def create_membership(
        self,
        *,
        organization_id: int,
        user_id: int,
        role: str,
        invited_by_user_id: int | None = None,
    ) -> Membership:
        if role not in ROLES:
            raise ValueError(f"Unknown role: {role!r}")
        # Upsert on (organization_id, user_id): re-inviting a previously
        # soft-deleted member reactivates the existing row rather than
        # failing the UNIQUE constraint. The invite flow's contract.
        self._conn.execute(
            """INSERT INTO memberships
               (organization_id, user_id, role, invited_by_user_id)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(organization_id, user_id) DO UPDATE SET
                   role = excluded.role,
                   invited_by_user_id = excluded.invited_by_user_id,
                   joined_at = datetime('now'),
                   deleted_at = NULL""",
            (organization_id, user_id, role, invited_by_user_id),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM memberships WHERE organization_id = ? AND user_id = ?",
            (organization_id, user_id),
        ).fetchone()
        return _row_to_membership(row)

    def get_membership(self, organization_id: int, user_id: int) -> Membership | None:
        row = self._conn.execute(
            "SELECT * FROM memberships "
            "WHERE organization_id = ? AND user_id = ? AND deleted_at IS NULL",
            (organization_id, user_id),
        ).fetchone()
        return _row_to_membership(row) if row else None

    def list_memberships_for_user(self, user_id: int) -> list[Membership]:
        rows = self._conn.execute(
            "SELECT * FROM memberships "
            "WHERE user_id = ? AND deleted_at IS NULL "
            "ORDER BY joined_at DESC, id DESC",
            (user_id,),
        ).fetchall()
        return [_row_to_membership(row) for row in rows]

    def list_memberships_for_org(self, organization_id: int) -> list[Membership]:
        rows = self._conn.execute(
            "SELECT * FROM memberships "
            "WHERE organization_id = ? AND deleted_at IS NULL "
            "ORDER BY joined_at ASC, id ASC",
            (organization_id,),
        ).fetchall()
        return [_row_to_membership(row) for row in rows]

    def update_membership_role(self, organization_id: int, user_id: int, role: str) -> bool:
        if role not in ROLES:
            raise ValueError(f"Unknown role: {role!r}")
        cursor = self._conn.execute(
            "UPDATE memberships SET role = ? "
            "WHERE organization_id = ? AND user_id = ? AND deleted_at IS NULL",
            (role, organization_id, user_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def soft_delete_membership(self, organization_id: int, user_id: int) -> bool:
        cursor = self._conn.execute(
            "UPDATE memberships SET deleted_at = datetime('now') "
            "WHERE organization_id = ? AND user_id = ? AND deleted_at IS NULL",
            (organization_id, user_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # --- Sessions ---

    def create_session(
        self,
        *,
        user_id: int | None = None,
        data: dict[str, Any] | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        ttl_seconds: int = 604800,
    ) -> Session:
        session_id = secrets.token_urlsafe(32)
        now = datetime.now(tz=timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        self._conn.execute(
            """INSERT INTO sessions
               (id, user_id, data_json, ip, user_agent, created_at, last_seen_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                user_id,
                json.dumps(data) if data else None,
                ip,
                user_agent,
                now.isoformat(),
                now.isoformat(),
                expires.isoformat(),
            ),
        )
        self._conn.commit()
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return _row_to_session(row)

    def get_session(self, session_id: str) -> Session | None:
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return _row_to_session(row) if row else None

    def touch_session(
        self,
        session_id: str,
        *,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> bool:
        # Always update last_seen_at; conditionally update ip / user_agent.
        sets = ["last_seen_at = ?"]
        params: list[Any] = [datetime.now(tz=timezone.utc).isoformat()]
        if ip is not None:
            sets.append("ip = ?")
            params.append(ip)
        if user_agent is not None:
            sets.append("user_agent = ?")
            params.append(user_agent)
        params.append(session_id)
        cursor = self._conn.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE id = ? AND revoked_at IS NULL",
            params,
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def revoke_session(self, session_id: str) -> bool:
        cursor = self._conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (datetime.now(tz=timezone.utc).isoformat(), session_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_sessions_for_user(self, user_id: int) -> list[Session]:
        rows = self._conn.execute(
            "SELECT * FROM sessions "
            "WHERE user_id = ? AND revoked_at IS NULL "
            "ORDER BY last_seen_at DESC, id DESC",
            (user_id,),
        ).fetchall()
        return [_row_to_session(row) for row in rows]

    def purge_expired_sessions(self) -> int:
        cursor = self._conn.execute(
            "DELETE FROM sessions WHERE expires_at < ?",
            (datetime.now(tz=timezone.utc).isoformat(),),
        )
        self._conn.commit()
        return cursor.rowcount

    # --- Patients (scope-enforced) ---

    def create_patient(
        self,
        scope: Scope,
        *,
        first_name: str,
        last_name: str,
        middle_name: str | None = None,
        date_of_birth: str | None = None,
        sex: str | None = None,
        mrn: str | None = None,
        preferred_language: str | None = None,
        pronouns: str | None = None,
        phone: str | None = None,
        email: str | None = None,
        address_line1: str | None = None,
        address_line2: str | None = None,
        address_city: str | None = None,
        address_state: str | None = None,
        address_zip: str | None = None,
        emergency_contact_name: str | None = None,
        emergency_contact_phone: str | None = None,
        notes: str | None = None,
        created_by_user_id: int | None = None,
    ) -> Patient:
        # scope_sql_clause raises ScopeRequired on anonymous — the explicit
        # scope_user_id / scope_organization_id columns below pick up the
        # correct value from the Scope directly.
        scope_sql_clause(scope)  # guard-only
        cursor = self._conn.execute(
            """INSERT INTO patients
               (scope_user_id, scope_organization_id, first_name, last_name,
                middle_name, date_of_birth, sex, mrn, preferred_language,
                pronouns, phone, email, address_line1, address_line2,
                address_city, address_state, address_zip,
                emergency_contact_name, emergency_contact_phone, notes,
                created_by_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                scope.user_id if scope.is_solo else None,
                scope.organization_id if scope.is_org else None,
                first_name,
                last_name,
                middle_name,
                date_of_birth,
                sex,
                mrn,
                preferred_language,
                pronouns,
                phone,
                email,
                address_line1,
                address_line2,
                address_city,
                address_state,
                address_zip,
                emergency_contact_name,
                emergency_contact_phone,
                notes,
                created_by_user_id,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM patients WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_patient(row)

    def get_patient(self, scope: Scope, patient_id: int) -> Patient | None:
        clause, params = scope_sql_clause(scope)
        row = self._conn.execute(
            f"SELECT * FROM patients WHERE id = ? AND {clause} AND deleted_at IS NULL",
            [patient_id, *params],
        ).fetchone()
        return _row_to_patient(row) if row else None

    def list_patients(
        self,
        scope: Scope,
        *,
        search: str | None = None,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Patient]:
        clause, params = scope_sql_clause(scope)
        where_parts = [clause]
        if not include_deleted:
            where_parts.append("deleted_at IS NULL")
        if search:
            # LIKE on last_name, first_name, or mrn — escape wildcards.
            term = f"%{_escape_like(search.strip())}%"
            where_parts.append(
                "(last_name LIKE ? ESCAPE '\\' OR first_name LIKE ? ESCAPE '\\' "
                "OR mrn LIKE ? ESCAPE '\\')"
            )
            params.extend([term, term, term])
        sql = (
            f"SELECT * FROM patients WHERE {' AND '.join(where_parts)} "
            "ORDER BY last_name ASC, first_name ASC, id ASC LIMIT ? OFFSET ?"
        )
        params.extend([int(limit), int(offset)])
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_patient(r) for r in rows]

    def update_patient(
        self,
        scope: Scope,
        patient_id: int,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        middle_name: str | None = None,
        date_of_birth: str | None = None,
        sex: str | None = None,
        mrn: str | None = None,
        preferred_language: str | None = None,
        pronouns: str | None = None,
        phone: str | None = None,
        email: str | None = None,
        address_line1: str | None = None,
        address_line2: str | None = None,
        address_city: str | None = None,
        address_state: str | None = None,
        address_zip: str | None = None,
        emergency_contact_name: str | None = None,
        emergency_contact_phone: str | None = None,
        notes: str | None = None,
    ) -> Patient | None:
        # Only pass-through the fields the caller actually set. None means
        # "don't touch" — the "clear a field" use case goes through a
        # dedicated method on the route layer (Phase 2) to stay explicit.
        fields: dict[str, str | None] = {
            k: v
            for k, v in {
                "first_name": first_name,
                "last_name": last_name,
                "middle_name": middle_name,
                "date_of_birth": date_of_birth,
                "sex": sex,
                "mrn": mrn,
                "preferred_language": preferred_language,
                "pronouns": pronouns,
                "phone": phone,
                "email": email,
                "address_line1": address_line1,
                "address_line2": address_line2,
                "address_city": address_city,
                "address_state": address_state,
                "address_zip": address_zip,
                "emergency_contact_name": emergency_contact_name,
                "emergency_contact_phone": emergency_contact_phone,
                "notes": notes,
            }.items()
            if v is not None
        }
        if not fields:
            return self.get_patient(scope, patient_id)

        clause, scope_params = scope_sql_clause(scope)
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        cursor = self._conn.execute(
            f"UPDATE patients SET {set_clause}, updated_at = datetime('now') "
            f"WHERE id = ? AND {clause} AND deleted_at IS NULL",
            [*fields.values(), patient_id, *scope_params],
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_patient(scope, patient_id)

    def soft_delete_patient(self, scope: Scope, patient_id: int) -> bool:
        clause, params = scope_sql_clause(scope)
        cursor = self._conn.execute(
            f"UPDATE patients SET deleted_at = datetime('now') "
            f"WHERE id = ? AND {clause} AND deleted_at IS NULL",
            [patient_id, *params],
        )
        self._conn.commit()
        return cursor.rowcount > 0

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
            appt_suite=row["appt_suite"] if "appt_suite" in row.keys() else None,
            appt_phone=row["appt_phone"] if "appt_phone" in row.keys() else None,
            appt_fax=row["appt_fax"] if "appt_fax" in row.keys() else None,
            is_televisit=bool(row["is_televisit"]) if "is_televisit" in row.keys() else False,
            enrichment_json=row["enrichment_json"] if "enrichment_json" in row.keys() else None,
            saved_at=datetime.fromisoformat(row["saved_at"]) if row["saved_at"] else None,
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
        )
