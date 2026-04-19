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
from docstats.domain.imports import (
    IMPORT_ROW_STATUS_VALUES,
    IMPORT_STATUS_VALUES,
    CsvImport,
    CsvImportRow,
)
from docstats.domain.orgs import ROLES, Membership, Organization
from docstats.domain.patients import Patient
from docstats.domain.reference import (
    PLAN_TYPE_VALUES,
    RULE_SOURCE_VALUES,
    InsurancePlan,
    PayerRule,
    SpecialtyRule,
)
from docstats.domain.referrals import (
    ATTACHMENT_KIND_VALUES,
    AUTH_STATUS_VALUES,
    EVENT_TYPE_VALUES,
    EXTERNAL_SOURCE_VALUES,
    RECEIVED_VIA_VALUES,
    SOURCE_VALUES,
    STATUS_VALUES,
    URGENCY_VALUES,
    Referral,
    ReferralAllergy,
    ReferralAttachment,
    ReferralDiagnosis,
    ReferralEvent,
    ReferralMedication,
    ReferralResponse,
)
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


def _row_to_referral(row: sqlite3.Row) -> Referral:
    """Convert a SQLite referrals row into a Referral model."""
    created = _parse_sqlite_utc(row["created_at"])
    updated = _parse_sqlite_utc(row["updated_at"])
    assert created is not None and updated is not None
    return Referral(
        id=int(row["id"]),
        scope_user_id=row["scope_user_id"],
        scope_organization_id=row["scope_organization_id"],
        patient_id=int(row["patient_id"]),
        referring_provider_npi=row["referring_provider_npi"],
        referring_provider_name=row["referring_provider_name"],
        referring_organization=row["referring_organization"],
        receiving_provider_npi=row["receiving_provider_npi"],
        receiving_organization_name=row["receiving_organization_name"],
        specialty_code=row["specialty_code"],
        specialty_desc=row["specialty_desc"],
        reason=row["reason"],
        clinical_question=row["clinical_question"],
        urgency=row["urgency"],
        requested_service=row["requested_service"],
        diagnosis_primary_icd=row["diagnosis_primary_icd"],
        diagnosis_primary_text=row["diagnosis_primary_text"],
        payer_plan_id=row["payer_plan_id"],
        authorization_number=row["authorization_number"],
        authorization_status=row["authorization_status"],
        status=row["status"],
        assigned_to_user_id=row["assigned_to_user_id"],
        external_reference_id=row["external_reference_id"],
        external_source=row["external_source"],
        created_by_user_id=row["created_by_user_id"],
        created_at=created,
        updated_at=updated,
        deleted_at=_parse_sqlite_utc(row["deleted_at"]),
    )


def _row_to_referral_event(row: sqlite3.Row) -> ReferralEvent:
    """Convert a SQLite referral_events row into a ReferralEvent."""
    created = _parse_sqlite_utc(row["created_at"])
    assert created is not None
    return ReferralEvent(
        id=int(row["id"]),
        referral_id=int(row["referral_id"]),
        event_type=row["event_type"],
        from_value=row["from_value"],
        to_value=row["to_value"],
        actor_user_id=row["actor_user_id"],
        note=row["note"],
        created_at=created,
    )


def _row_to_referral_diagnosis(row: sqlite3.Row) -> ReferralDiagnosis:
    created = _parse_sqlite_utc(row["created_at"])
    assert created is not None
    return ReferralDiagnosis(
        id=int(row["id"]),
        referral_id=int(row["referral_id"]),
        icd10_code=row["icd10_code"],
        icd10_desc=row["icd10_desc"],
        is_primary=bool(row["is_primary"]),
        source=row["source"],
        created_at=created,
    )


def _row_to_referral_medication(row: sqlite3.Row) -> ReferralMedication:
    created = _parse_sqlite_utc(row["created_at"])
    assert created is not None
    return ReferralMedication(
        id=int(row["id"]),
        referral_id=int(row["referral_id"]),
        name=row["name"],
        dose=row["dose"],
        route=row["route"],
        frequency=row["frequency"],
        source=row["source"],
        created_at=created,
    )


def _row_to_referral_allergy(row: sqlite3.Row) -> ReferralAllergy:
    created = _parse_sqlite_utc(row["created_at"])
    assert created is not None
    return ReferralAllergy(
        id=int(row["id"]),
        referral_id=int(row["referral_id"]),
        substance=row["substance"],
        reaction=row["reaction"],
        severity=row["severity"],
        source=row["source"],
        created_at=created,
    )


def _row_to_referral_attachment(row: sqlite3.Row) -> ReferralAttachment:
    created = _parse_sqlite_utc(row["created_at"])
    assert created is not None
    return ReferralAttachment(
        id=int(row["id"]),
        referral_id=int(row["referral_id"]),
        kind=row["kind"],
        label=row["label"],
        date_of_service=row["date_of_service"],
        storage_ref=row["storage_ref"],
        checklist_only=bool(row["checklist_only"]),
        source=row["source"],
        created_at=created,
    )


def _row_to_csv_import(row: sqlite3.Row) -> CsvImport:
    created = _parse_sqlite_utc(row["created_at"])
    updated = _parse_sqlite_utc(row["updated_at"])
    assert created is not None and updated is not None
    return CsvImport(
        id=int(row["id"]),
        scope_user_id=row["scope_user_id"],
        scope_organization_id=row["scope_organization_id"],
        uploaded_by_user_id=row["uploaded_by_user_id"],
        original_filename=row["original_filename"],
        row_count=int(row["row_count"]),
        status=row["status"],
        mapping=json.loads(row["mapping"]) if row["mapping"] else {},
        error_report=json.loads(row["error_report"]) if row["error_report"] else {},
        created_at=created,
        updated_at=updated,
    )


def _row_to_csv_import_row(row: sqlite3.Row) -> CsvImportRow:
    created = _parse_sqlite_utc(row["created_at"])
    updated = _parse_sqlite_utc(row["updated_at"])
    assert created is not None and updated is not None
    return CsvImportRow(
        id=int(row["id"]),
        import_id=int(row["import_id"]),
        row_index=int(row["row_index"]),
        raw_json=json.loads(row["raw_json"]) if row["raw_json"] else {},
        validation_errors=json.loads(row["validation_errors"]) if row["validation_errors"] else {},
        referral_id=row["referral_id"],
        status=row["status"],
        created_at=created,
        updated_at=updated,
    )


def _row_to_referral_response(row: sqlite3.Row) -> ReferralResponse:
    created = _parse_sqlite_utc(row["created_at"])
    updated = _parse_sqlite_utc(row["updated_at"])
    assert created is not None and updated is not None
    return ReferralResponse(
        id=int(row["id"]),
        referral_id=int(row["referral_id"]),
        appointment_date=row["appointment_date"],
        consult_completed=bool(row["consult_completed"]),
        recommendations_text=row["recommendations_text"],
        attached_consult_note_ref=row["attached_consult_note_ref"],
        received_via=row["received_via"],
        recorded_by_user_id=row["recorded_by_user_id"],
        created_at=created,
        updated_at=updated,
    )


def _row_to_insurance_plan(row: sqlite3.Row) -> InsurancePlan:
    created = _parse_sqlite_utc(row["created_at"])
    updated = _parse_sqlite_utc(row["updated_at"])
    assert created is not None and updated is not None
    return InsurancePlan(
        id=int(row["id"]),
        scope_user_id=row["scope_user_id"],
        scope_organization_id=row["scope_organization_id"],
        payer_name=row["payer_name"],
        plan_name=row["plan_name"],
        plan_type=row["plan_type"],
        member_id_pattern=row["member_id_pattern"],
        group_id_pattern=row["group_id_pattern"],
        requires_referral=bool(row["requires_referral"]),
        requires_prior_auth=bool(row["requires_prior_auth"]),
        notes=row["notes"],
        created_at=created,
        updated_at=updated,
        deleted_at=_parse_sqlite_utc(row["deleted_at"]),
    )


def _row_to_specialty_rule(row: sqlite3.Row) -> SpecialtyRule:
    created = _parse_sqlite_utc(row["created_at"])
    updated = _parse_sqlite_utc(row["updated_at"])
    assert created is not None and updated is not None
    return SpecialtyRule(
        id=int(row["id"]),
        organization_id=row["organization_id"],
        specialty_code=row["specialty_code"],
        display_name=row["display_name"],
        required_fields=json.loads(row["required_fields"]) if row["required_fields"] else {},
        recommended_attachments=json.loads(row["recommended_attachments"])
        if row["recommended_attachments"]
        else {},
        intake_questions=json.loads(row["intake_questions"]) if row["intake_questions"] else {},
        urgency_red_flags=json.loads(row["urgency_red_flags"]) if row["urgency_red_flags"] else {},
        common_rejection_reasons=json.loads(row["common_rejection_reasons"])
        if row["common_rejection_reasons"]
        else {},
        source=row["source"],
        version_id=int(row["version_id"]),
        created_at=created,
        updated_at=updated,
    )


def _row_to_payer_rule(row: sqlite3.Row) -> PayerRule:
    created = _parse_sqlite_utc(row["created_at"])
    updated = _parse_sqlite_utc(row["updated_at"])
    assert created is not None and updated is not None
    return PayerRule(
        id=int(row["id"]),
        organization_id=row["organization_id"],
        payer_key=row["payer_key"],
        display_name=row["display_name"],
        referral_required=bool(row["referral_required"]),
        auth_required_services=json.loads(row["auth_required_services"])
        if row["auth_required_services"]
        else {},
        auth_typical_turnaround_days=row["auth_typical_turnaround_days"],
        records_required=json.loads(row["records_required"]) if row["records_required"] else {},
        notes=row["notes"],
        source=row["source"],
        version_id=int(row["version_id"]),
        created_at=created,
        updated_at=updated,
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
        self._migrate_referrals()
        self._migrate_referral_clinical()
        self._migrate_referral_responses()
        self._migrate_reference_data()
        self._migrate_csv_imports()

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

    def _migrate_referrals(self) -> None:
        """Create referrals + referral_events tables (Phase 1.B)."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS referrals (
                id                            INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_user_id                 INTEGER REFERENCES users(id) ON DELETE CASCADE,
                scope_organization_id         INTEGER REFERENCES organizations(id) ON DELETE CASCADE,
                patient_id                    INTEGER NOT NULL REFERENCES patients(id) ON DELETE RESTRICT,
                referring_provider_npi        TEXT,
                referring_provider_name       TEXT,
                referring_organization        TEXT,
                receiving_provider_npi        TEXT,
                receiving_organization_name   TEXT,
                specialty_code                TEXT,
                specialty_desc                TEXT,
                reason                        TEXT,
                clinical_question             TEXT,
                urgency                       TEXT NOT NULL DEFAULT 'routine'
                    CHECK (urgency IN ('routine','priority','urgent','stat')),
                requested_service             TEXT,
                diagnosis_primary_icd         TEXT,
                diagnosis_primary_text        TEXT,
                payer_plan_id                 INTEGER,
                authorization_number          TEXT,
                authorization_status          TEXT NOT NULL DEFAULT 'na_unknown'
                    CHECK (authorization_status IN
                           ('not_required','required_pending','obtained','denied','na_unknown')),
                status                        TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN
                           ('draft','ready','sent','awaiting_records','awaiting_auth',
                            'scheduled','rejected','completed','cancelled')),
                assigned_to_user_id           INTEGER REFERENCES users(id) ON DELETE SET NULL,
                external_reference_id         TEXT,
                external_source               TEXT NOT NULL DEFAULT 'manual'
                    CHECK (external_source IN ('manual','bulk_csv','api')),
                created_by_user_id            INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at                    TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at                    TEXT NOT NULL DEFAULT (datetime('now')),
                deleted_at                    TEXT,
                CHECK (
                    (scope_user_id IS NOT NULL AND scope_organization_id IS NULL)
                    OR (scope_user_id IS NULL AND scope_organization_id IS NOT NULL)
                )
            );
            CREATE INDEX IF NOT EXISTS idx_referrals_scope_user_status
                ON referrals(scope_user_id, status, updated_at DESC) WHERE deleted_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_referrals_scope_org_status
                ON referrals(scope_organization_id, status, updated_at DESC) WHERE deleted_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_referrals_patient
                ON referrals(patient_id, created_at DESC) WHERE deleted_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_referrals_assignee
                ON referrals(assigned_to_user_id, status, updated_at DESC)
                WHERE assigned_to_user_id IS NOT NULL AND deleted_at IS NULL;

            CREATE TABLE IF NOT EXISTS referral_events (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                referral_id      INTEGER NOT NULL REFERENCES referrals(id) ON DELETE CASCADE,
                event_type       TEXT NOT NULL
                    CHECK (event_type IN
                           ('created','status_changed','field_edited','exported','sent',
                            'response_received','note_added','assigned','unassigned')),
                from_value       TEXT,
                to_value         TEXT,
                actor_user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
                note             TEXT,
                created_at       TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_referral_events_referral
                ON referral_events(referral_id, created_at DESC, id DESC);
        """)
        self._conn.commit()

    def _migrate_referral_clinical(self) -> None:
        """Create the four clinical sub-tables (Phase 1.C)."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS referral_diagnoses (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                referral_id  INTEGER NOT NULL REFERENCES referrals(id) ON DELETE CASCADE,
                icd10_code   TEXT NOT NULL,
                icd10_desc   TEXT,
                is_primary   INTEGER NOT NULL DEFAULT 0,
                source       TEXT NOT NULL DEFAULT 'user_entered'
                    CHECK (source IN ('user_entered','imported_csv','nppes','ai_draft','carry_forward','ehr_import')),
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_referral_diagnoses_referral
                ON referral_diagnoses(referral_id, is_primary DESC, id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_diagnoses_one_primary
                ON referral_diagnoses(referral_id) WHERE is_primary = 1;

            CREATE TABLE IF NOT EXISTS referral_medications (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                referral_id  INTEGER NOT NULL REFERENCES referrals(id) ON DELETE CASCADE,
                name         TEXT NOT NULL,
                dose         TEXT,
                route        TEXT,
                frequency    TEXT,
                source       TEXT NOT NULL DEFAULT 'user_entered'
                    CHECK (source IN ('user_entered','imported_csv','nppes','ai_draft','carry_forward','ehr_import')),
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_referral_medications_referral
                ON referral_medications(referral_id, id);

            CREATE TABLE IF NOT EXISTS referral_allergies (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                referral_id  INTEGER NOT NULL REFERENCES referrals(id) ON DELETE CASCADE,
                substance    TEXT NOT NULL,
                reaction     TEXT,
                severity     TEXT,
                source       TEXT NOT NULL DEFAULT 'user_entered'
                    CHECK (source IN ('user_entered','imported_csv','nppes','ai_draft','carry_forward','ehr_import')),
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_referral_allergies_referral
                ON referral_allergies(referral_id, id);

            CREATE TABLE IF NOT EXISTS referral_attachments (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                referral_id       INTEGER NOT NULL REFERENCES referrals(id) ON DELETE CASCADE,
                kind              TEXT NOT NULL
                    CHECK (kind IN ('lab','imaging','note','procedure','medication_list','problem_list','other')),
                label             TEXT NOT NULL,
                date_of_service   TEXT,
                storage_ref       TEXT,
                checklist_only    INTEGER NOT NULL DEFAULT 1,
                source            TEXT NOT NULL DEFAULT 'user_entered'
                    CHECK (source IN ('user_entered','imported_csv','nppes','ai_draft','carry_forward','ehr_import')),
                created_at        TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_referral_attachments_referral
                ON referral_attachments(referral_id, id);
        """)
        self._conn.commit()

    def _migrate_referral_responses(self) -> None:
        """Create the referral_responses table (Phase 1.D)."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS referral_responses (
                id                           INTEGER PRIMARY KEY AUTOINCREMENT,
                referral_id                  INTEGER NOT NULL REFERENCES referrals(id) ON DELETE CASCADE,
                appointment_date             TEXT,
                consult_completed            INTEGER NOT NULL DEFAULT 0,
                recommendations_text         TEXT,
                attached_consult_note_ref    TEXT,
                received_via                 TEXT NOT NULL DEFAULT 'manual'
                    CHECK (received_via IN ('fax','portal','email','phone','manual','api')),
                recorded_by_user_id          INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at                   TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at                   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_referral_responses_referral
                ON referral_responses(referral_id, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_referral_responses_completed
                ON referral_responses(referral_id) WHERE consult_completed = 1;
        """)
        self._conn.commit()

    def _migrate_reference_data(self) -> None:
        """Create insurance_plans, specialty_rules, payer_rules (Phase 1.E)."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS insurance_plans (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_user_id            INTEGER REFERENCES users(id) ON DELETE CASCADE,
                scope_organization_id    INTEGER REFERENCES organizations(id) ON DELETE CASCADE,
                payer_name               TEXT NOT NULL,
                plan_name                TEXT,
                plan_type                TEXT NOT NULL DEFAULT 'other'
                    CHECK (plan_type IN
                           ('hmo','ppo','pos','epo','medicare','medicare_advantage',
                            'medicaid','tricare','aca_marketplace','self_pay','other')),
                member_id_pattern        TEXT,
                group_id_pattern         TEXT,
                requires_referral        INTEGER NOT NULL DEFAULT 0,
                requires_prior_auth      INTEGER NOT NULL DEFAULT 0,
                notes                    TEXT,
                created_at               TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
                deleted_at               TEXT,
                CHECK (
                    (scope_user_id IS NOT NULL AND scope_organization_id IS NULL)
                    OR (scope_user_id IS NULL AND scope_organization_id IS NOT NULL)
                )
            );
            CREATE INDEX IF NOT EXISTS idx_insurance_plans_scope_user
                ON insurance_plans(scope_user_id, payer_name) WHERE deleted_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_insurance_plans_scope_org
                ON insurance_plans(scope_organization_id, payer_name) WHERE deleted_at IS NULL;

            CREATE TABLE IF NOT EXISTS specialty_rules (
                id                         INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id            INTEGER REFERENCES organizations(id) ON DELETE CASCADE,
                specialty_code             TEXT NOT NULL,
                display_name               TEXT,
                required_fields            TEXT NOT NULL DEFAULT '{}',
                recommended_attachments    TEXT NOT NULL DEFAULT '{}',
                intake_questions           TEXT NOT NULL DEFAULT '{}',
                urgency_red_flags          TEXT NOT NULL DEFAULT '{}',
                common_rejection_reasons   TEXT NOT NULL DEFAULT '{}',
                source                     TEXT NOT NULL DEFAULT 'seed'
                    CHECK (source IN ('seed','admin_override')),
                version_id                 INTEGER NOT NULL DEFAULT 1,
                created_at                 TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at                 TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_specialty_rules_global_code
                ON specialty_rules(specialty_code) WHERE organization_id IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_specialty_rules_org_code
                ON specialty_rules(organization_id, specialty_code)
                WHERE organization_id IS NOT NULL;

            CREATE TABLE IF NOT EXISTS payer_rules (
                id                              INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id                 INTEGER REFERENCES organizations(id) ON DELETE CASCADE,
                payer_key                       TEXT NOT NULL,
                display_name                    TEXT,
                referral_required               INTEGER NOT NULL DEFAULT 0,
                auth_required_services          TEXT NOT NULL DEFAULT '{}',
                auth_typical_turnaround_days    INTEGER,
                records_required                TEXT NOT NULL DEFAULT '{}',
                notes                           TEXT,
                source                          TEXT NOT NULL DEFAULT 'seed'
                    CHECK (source IN ('seed','admin_override')),
                version_id                      INTEGER NOT NULL DEFAULT 1,
                created_at                      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at                      TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_payer_rules_global_key
                ON payer_rules(payer_key) WHERE organization_id IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_payer_rules_org_key
                ON payer_rules(organization_id, payer_key)
                WHERE organization_id IS NOT NULL;
        """)
        self._conn.commit()

    def _migrate_csv_imports(self) -> None:
        """Create csv_imports + csv_import_rows (Phase 1.F)."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS csv_imports (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_user_id            INTEGER REFERENCES users(id) ON DELETE CASCADE,
                scope_organization_id    INTEGER REFERENCES organizations(id) ON DELETE CASCADE,
                uploaded_by_user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
                original_filename        TEXT NOT NULL,
                row_count                INTEGER NOT NULL DEFAULT 0,
                status                   TEXT NOT NULL DEFAULT 'uploaded'
                    CHECK (status IN ('uploaded','mapped','validated','committed','failed')),
                mapping                  TEXT NOT NULL DEFAULT '{}',
                error_report             TEXT NOT NULL DEFAULT '{}',
                created_at               TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
                CHECK (
                    (scope_user_id IS NOT NULL AND scope_organization_id IS NULL)
                    OR (scope_user_id IS NULL AND scope_organization_id IS NOT NULL)
                )
            );
            CREATE INDEX IF NOT EXISTS idx_csv_imports_scope_user
                ON csv_imports(scope_user_id, created_at DESC)
                WHERE scope_user_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_csv_imports_scope_org
                ON csv_imports(scope_organization_id, created_at DESC)
                WHERE scope_organization_id IS NOT NULL;

            CREATE TABLE IF NOT EXISTS csv_import_rows (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id           INTEGER NOT NULL REFERENCES csv_imports(id) ON DELETE CASCADE,
                row_index           INTEGER NOT NULL,
                raw_json            TEXT NOT NULL DEFAULT '{}',
                validation_errors   TEXT NOT NULL DEFAULT '{}',
                referral_id         INTEGER REFERENCES referrals(id) ON DELETE SET NULL,
                status              TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','valid','error','committed','skipped')),
                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_csv_import_rows_import
                ON csv_import_rows(import_id, row_index);
            CREATE INDEX IF NOT EXISTS idx_csv_import_rows_status
                ON csv_import_rows(import_id, status)
                WHERE status IN ('error','pending');
            CREATE UNIQUE INDEX IF NOT EXISTS idx_csv_import_rows_unique_index
                ON csv_import_rows(import_id, row_index);
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

    # --- Referrals (scope-enforced; patient_id scope-matched) ---

    def create_referral(
        self,
        scope: Scope,
        *,
        patient_id: int,
        referring_provider_npi: str | None = None,
        referring_provider_name: str | None = None,
        referring_organization: str | None = None,
        receiving_provider_npi: str | None = None,
        receiving_organization_name: str | None = None,
        specialty_code: str | None = None,
        specialty_desc: str | None = None,
        reason: str | None = None,
        clinical_question: str | None = None,
        urgency: str = "routine",
        requested_service: str | None = None,
        diagnosis_primary_icd: str | None = None,
        diagnosis_primary_text: str | None = None,
        payer_plan_id: int | None = None,
        authorization_number: str | None = None,
        authorization_status: str = "na_unknown",
        status: str = "draft",
        assigned_to_user_id: int | None = None,
        external_reference_id: str | None = None,
        external_source: str = "manual",
        created_by_user_id: int | None = None,
    ) -> Referral:
        scope_sql_clause(scope)  # raises ScopeRequired on anonymous
        if urgency not in URGENCY_VALUES:
            raise ValueError(f"Unknown urgency: {urgency!r}")
        if authorization_status not in AUTH_STATUS_VALUES:
            raise ValueError(f"Unknown authorization_status: {authorization_status!r}")
        if status not in STATUS_VALUES:
            raise ValueError(f"Unknown status: {status!r}")
        if external_source not in EXTERNAL_SOURCE_VALUES:
            raise ValueError(f"Unknown external_source: {external_source!r}")
        # Patient must be readable in the same scope — prevents cross-tenant
        # FK forgery (e.g. referring to a patient in another org).
        if self.get_patient(scope, patient_id) is None:
            raise ValueError(f"Patient {patient_id} not found in scope or soft-deleted")
        # payer_plan_id follows the same rule: insurance_plans is scope-owned,
        # so refuse to attach a plan that isn't readable from this scope.
        if payer_plan_id is not None and self.get_insurance_plan(scope, payer_plan_id) is None:
            raise ValueError(
                f"payer_plan_id={payer_plan_id} not accessible from the caller's scope"
            )

        # Insert referral + seed the ``created`` event atomically. Every
        # referral must have at least one event from t=0 — a partial write
        # (referral landed, event didn't) would break the "append-only
        # timeline" contract.
        with self._conn:
            cursor = self._conn.execute(
                """INSERT INTO referrals (
                    scope_user_id, scope_organization_id, patient_id,
                    referring_provider_npi, referring_provider_name, referring_organization,
                    receiving_provider_npi, receiving_organization_name,
                    specialty_code, specialty_desc,
                    reason, clinical_question, urgency, requested_service,
                    diagnosis_primary_icd, diagnosis_primary_text,
                    payer_plan_id, authorization_number, authorization_status,
                    status, assigned_to_user_id,
                    external_reference_id, external_source, created_by_user_id
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                (
                    scope.user_id if scope.is_solo else None,
                    scope.organization_id if scope.is_org else None,
                    patient_id,
                    referring_provider_npi,
                    referring_provider_name,
                    referring_organization,
                    receiving_provider_npi,
                    receiving_organization_name,
                    specialty_code,
                    specialty_desc,
                    reason,
                    clinical_question,
                    urgency,
                    requested_service,
                    diagnosis_primary_icd,
                    diagnosis_primary_text,
                    payer_plan_id,
                    authorization_number,
                    authorization_status,
                    status,
                    assigned_to_user_id,
                    external_reference_id,
                    external_source,
                    created_by_user_id,
                ),
            )
            referral_id = cursor.lastrowid
            assert referral_id is not None
            self._conn.execute(
                "INSERT INTO referral_events (referral_id, event_type, to_value, actor_user_id) "
                "VALUES (?, 'created', ?, ?)",
                (referral_id, status, created_by_user_id),
            )
        row = self._conn.execute("SELECT * FROM referrals WHERE id = ?", (referral_id,)).fetchone()
        return _row_to_referral(row)

    def get_referral(self, scope: Scope, referral_id: int) -> Referral | None:
        clause, params = scope_sql_clause(scope)
        row = self._conn.execute(
            f"SELECT * FROM referrals WHERE id = ? AND {clause} AND deleted_at IS NULL",
            [referral_id, *params],
        ).fetchone()
        return _row_to_referral(row) if row else None

    def list_referrals(
        self,
        scope: Scope,
        *,
        patient_id: int | None = None,
        status: str | None = None,
        urgency: str | None = None,
        assigned_to_user_id: int | None = None,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Referral]:
        clause, params = scope_sql_clause(scope)
        where_parts = [clause]
        if not include_deleted:
            where_parts.append("deleted_at IS NULL")
        if patient_id is not None:
            where_parts.append("patient_id = ?")
            params.append(patient_id)
        if status is not None:
            where_parts.append("status = ?")
            params.append(status)
        if urgency is not None:
            where_parts.append("urgency = ?")
            params.append(urgency)
        if assigned_to_user_id is not None:
            where_parts.append("assigned_to_user_id = ?")
            params.append(assigned_to_user_id)
        sql = (
            f"SELECT * FROM referrals WHERE {' AND '.join(where_parts)} "
            "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        params.extend([int(limit), int(offset)])
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_referral(r) for r in rows]

    def update_referral(
        self,
        scope: Scope,
        referral_id: int,
        *,
        referring_provider_npi: str | None = None,
        referring_provider_name: str | None = None,
        referring_organization: str | None = None,
        receiving_provider_npi: str | None = None,
        receiving_organization_name: str | None = None,
        specialty_code: str | None = None,
        specialty_desc: str | None = None,
        reason: str | None = None,
        clinical_question: str | None = None,
        urgency: str | None = None,
        requested_service: str | None = None,
        diagnosis_primary_icd: str | None = None,
        diagnosis_primary_text: str | None = None,
        payer_plan_id: int | None = None,
        authorization_number: str | None = None,
        authorization_status: str | None = None,
        assigned_to_user_id: int | None = None,
    ) -> Referral | None:
        if urgency is not None and urgency not in URGENCY_VALUES:
            raise ValueError(f"Unknown urgency: {urgency!r}")
        if authorization_status is not None and authorization_status not in AUTH_STATUS_VALUES:
            raise ValueError(f"Unknown authorization_status: {authorization_status!r}")
        if payer_plan_id is not None and self.get_insurance_plan(scope, payer_plan_id) is None:
            raise ValueError(
                f"payer_plan_id={payer_plan_id} not accessible from the caller's scope"
            )
        fields: dict[str, Any] = {
            k: v
            for k, v in {
                "referring_provider_npi": referring_provider_npi,
                "referring_provider_name": referring_provider_name,
                "referring_organization": referring_organization,
                "receiving_provider_npi": receiving_provider_npi,
                "receiving_organization_name": receiving_organization_name,
                "specialty_code": specialty_code,
                "specialty_desc": specialty_desc,
                "reason": reason,
                "clinical_question": clinical_question,
                "urgency": urgency,
                "requested_service": requested_service,
                "diagnosis_primary_icd": diagnosis_primary_icd,
                "diagnosis_primary_text": diagnosis_primary_text,
                "payer_plan_id": payer_plan_id,
                "authorization_number": authorization_number,
                "authorization_status": authorization_status,
                "assigned_to_user_id": assigned_to_user_id,
            }.items()
            if v is not None
        }
        if not fields:
            return self.get_referral(scope, referral_id)
        clause, scope_params = scope_sql_clause(scope)
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        cursor = self._conn.execute(
            f"UPDATE referrals SET {set_clause}, updated_at = datetime('now') "
            f"WHERE id = ? AND {clause} AND deleted_at IS NULL",
            [*fields.values(), referral_id, *scope_params],
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_referral(scope, referral_id)

    def set_referral_status(
        self,
        scope: Scope,
        referral_id: int,
        new_status: str,
    ) -> Referral | None:
        """Update status without validating the transition.

        State-machine validation lives in ``domain.referrals.require_transition``
        — route handlers call that first, then this. Storage stays dumb so
        repair scripts / migrations can set any valid enum value.
        """
        if new_status not in STATUS_VALUES:
            raise ValueError(f"Unknown status: {new_status!r}")
        clause, params = scope_sql_clause(scope)
        cursor = self._conn.execute(
            f"UPDATE referrals SET status = ?, updated_at = datetime('now') "
            f"WHERE id = ? AND {clause} AND deleted_at IS NULL",
            [new_status, referral_id, *params],
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_referral(scope, referral_id)

    def soft_delete_referral(self, scope: Scope, referral_id: int) -> bool:
        clause, params = scope_sql_clause(scope)
        cursor = self._conn.execute(
            f"UPDATE referrals SET deleted_at = datetime('now') "
            f"WHERE id = ? AND {clause} AND deleted_at IS NULL",
            [referral_id, *params],
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # Nullable headline fields safe to null directly. ``diagnosis_primary_icd``
    # / ``diagnosis_primary_text`` are intentionally NOT in this set — they
    # are denormalized from ``referral_diagnoses`` and must be changed by
    # flipping / deleting the ``is_primary`` sub-table row so the sync helper
    # keeps both columns in lockstep. Clearing them directly would break the
    # "sub-table is source of truth" invariant.
    _CLEARABLE_REFERRAL_FIELDS: frozenset[str] = frozenset(
        {
            "assigned_to_user_id",
            "authorization_number",
            "payer_plan_id",
            "external_reference_id",
        }
    )

    def clear_referral_field(self, scope: Scope, referral_id: int, field: str) -> Referral | None:
        if field not in self._CLEARABLE_REFERRAL_FIELDS:
            raise ValueError(f"Field {field!r} is not clearable on a referral")
        clause, scope_params = scope_sql_clause(scope)
        cursor = self._conn.execute(
            f"UPDATE referrals SET {field} = NULL, updated_at = datetime('now') "
            f"WHERE id = ? AND {clause} AND deleted_at IS NULL",
            [referral_id, *scope_params],
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_referral(scope, referral_id)

    # --- Referral events (append-only; scope-transitive) ---

    def record_referral_event(
        self,
        scope: Scope,
        referral_id: int,
        *,
        event_type: str,
        from_value: str | None = None,
        to_value: str | None = None,
        actor_user_id: int | None = None,
        note: str | None = None,
    ) -> ReferralEvent | None:
        if event_type not in EVENT_TYPE_VALUES:
            raise ValueError(f"Unknown event_type: {event_type!r}")
        # Scope-gate: the referral must be readable from this scope. Returning
        # None on miss avoids leaking the existence of out-of-scope referrals.
        if self.get_referral(scope, referral_id) is None:
            return None
        cursor = self._conn.execute(
            "INSERT INTO referral_events "
            "(referral_id, event_type, from_value, to_value, actor_user_id, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (referral_id, event_type, from_value, to_value, actor_user_id, note),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM referral_events WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_referral_event(row)

    def list_referral_events(
        self,
        scope: Scope,
        referral_id: int,
        *,
        limit: int = 100,
    ) -> list[ReferralEvent]:
        if self.get_referral(scope, referral_id) is None:
            return []
        rows = self._conn.execute(
            "SELECT * FROM referral_events WHERE referral_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (referral_id, int(limit)),
        ).fetchall()
        return [_row_to_referral_event(r) for r in rows]

    # --- Referral clinical sub-entities (scope-transitive via referral) ---

    # --- Diagnoses ---

    def _sync_referral_primary_diagnosis(self, referral_id: int) -> None:
        """Re-sync ``referrals.diagnosis_primary_{icd,text}`` to match the
        current ``is_primary=True`` row in ``referral_diagnoses``.

        Called whenever the primary bit on a diagnosis row is touched — by
        add with ``is_primary=True``, by update that toggles primary or
        edits the currently-primary row's code/desc, or by delete of the
        currently-primary row. If no primary row exists, the headline is
        cleared.

        Purely in-transaction: does NOT commit. Callers must wrap their
        mutation + this sync in ``with self._conn:`` so both the sub-table
        write and the headline update land atomically.
        """
        row = self._conn.execute(
            "SELECT icd10_code, icd10_desc FROM referral_diagnoses "
            "WHERE referral_id = ? AND is_primary = 1 LIMIT 1",
            (referral_id,),
        ).fetchone()
        if row is not None:
            self._conn.execute(
                "UPDATE referrals SET diagnosis_primary_icd = ?, "
                "diagnosis_primary_text = ?, updated_at = datetime('now') WHERE id = ?",
                (row["icd10_code"], row["icd10_desc"], referral_id),
            )
        else:
            self._conn.execute(
                "UPDATE referrals SET diagnosis_primary_icd = NULL, "
                "diagnosis_primary_text = NULL, updated_at = datetime('now') WHERE id = ?",
                (referral_id,),
            )

    def add_referral_diagnosis(
        self,
        scope: Scope,
        referral_id: int,
        *,
        icd10_code: str,
        icd10_desc: str | None = None,
        is_primary: bool = False,
        source: str = "user_entered",
    ) -> ReferralDiagnosis | None:
        if source not in SOURCE_VALUES:
            raise ValueError(f"Unknown source: {source!r}")
        if self.get_referral(scope, referral_id) is None:
            return None
        # Insert + headline sync in a single transaction so a process crash
        # between the two can't leave the sub-table out of sync with the
        # denormalized headline on ``referrals``.
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO referral_diagnoses "
                "(referral_id, icd10_code, icd10_desc, is_primary, source) "
                "VALUES (?, ?, ?, ?, ?)",
                (referral_id, icd10_code, icd10_desc, 1 if is_primary else 0, source),
            )
            if is_primary:
                self._sync_referral_primary_diagnosis(referral_id)
        row = self._conn.execute(
            "SELECT * FROM referral_diagnoses WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_referral_diagnosis(row)

    def list_referral_diagnoses(self, scope: Scope, referral_id: int) -> list[ReferralDiagnosis]:
        if self.get_referral(scope, referral_id) is None:
            return []
        rows = self._conn.execute(
            "SELECT * FROM referral_diagnoses WHERE referral_id = ? "
            "ORDER BY is_primary DESC, id ASC",
            (referral_id,),
        ).fetchall()
        return [_row_to_referral_diagnosis(r) for r in rows]

    def update_referral_diagnosis(
        self,
        scope: Scope,
        referral_id: int,
        diagnosis_id: int,
        *,
        icd10_code: str | None = None,
        icd10_desc: str | None = None,
        is_primary: bool | None = None,
        source: str | None = None,
    ) -> ReferralDiagnosis | None:
        if source is not None and source not in SOURCE_VALUES:
            raise ValueError(f"Unknown source: {source!r}")
        if self.get_referral(scope, referral_id) is None:
            return None
        pre = self._conn.execute(
            "SELECT is_primary FROM referral_diagnoses WHERE id = ? AND referral_id = ?",
            (diagnosis_id, referral_id),
        ).fetchone()
        was_primary = bool(pre["is_primary"]) if pre is not None else False
        fields: dict[str, Any] = {}
        if icd10_code is not None:
            fields["icd10_code"] = icd10_code
        if icd10_desc is not None:
            fields["icd10_desc"] = icd10_desc
        if is_primary is not None:
            fields["is_primary"] = 1 if is_primary else 0
        if source is not None:
            fields["source"] = source
        if not fields:
            row = self._conn.execute(
                "SELECT * FROM referral_diagnoses WHERE id = ? AND referral_id = ?",
                (diagnosis_id, referral_id),
            ).fetchone()
            return _row_to_referral_diagnosis(row) if row else None
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        # Update + headline sync atomically (same rationale as add_).
        with self._conn:
            cursor = self._conn.execute(
                f"UPDATE referral_diagnoses SET {set_clause} WHERE id = ? AND referral_id = ?",
                [*fields.values(), diagnosis_id, referral_id],
            )
            if cursor.rowcount == 0:
                return None
            # Re-sync if primary status touched this row, either by explicit
            # toggle or by an edit on the currently-primary row.
            if is_primary is not None or was_primary:
                self._sync_referral_primary_diagnosis(referral_id)
        row = self._conn.execute(
            "SELECT * FROM referral_diagnoses WHERE id = ?", (diagnosis_id,)
        ).fetchone()
        return _row_to_referral_diagnosis(row) if row else None

    def delete_referral_diagnosis(self, scope: Scope, referral_id: int, diagnosis_id: int) -> bool:
        if self.get_referral(scope, referral_id) is None:
            return False
        pre = self._conn.execute(
            "SELECT is_primary FROM referral_diagnoses WHERE id = ? AND referral_id = ?",
            (diagnosis_id, referral_id),
        ).fetchone()
        was_primary = bool(pre["is_primary"]) if pre is not None else False
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM referral_diagnoses WHERE id = ? AND referral_id = ?",
                (diagnosis_id, referral_id),
            )
            if cursor.rowcount > 0 and was_primary:
                self._sync_referral_primary_diagnosis(referral_id)
        return cursor.rowcount > 0

    # --- Medications ---

    def add_referral_medication(
        self,
        scope: Scope,
        referral_id: int,
        *,
        name: str,
        dose: str | None = None,
        route: str | None = None,
        frequency: str | None = None,
        source: str = "user_entered",
    ) -> ReferralMedication | None:
        if source not in SOURCE_VALUES:
            raise ValueError(f"Unknown source: {source!r}")
        if self.get_referral(scope, referral_id) is None:
            return None
        cursor = self._conn.execute(
            "INSERT INTO referral_medications "
            "(referral_id, name, dose, route, frequency, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (referral_id, name, dose, route, frequency, source),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM referral_medications WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_referral_medication(row)

    def list_referral_medications(self, scope: Scope, referral_id: int) -> list[ReferralMedication]:
        if self.get_referral(scope, referral_id) is None:
            return []
        rows = self._conn.execute(
            "SELECT * FROM referral_medications WHERE referral_id = ? ORDER BY id ASC",
            (referral_id,),
        ).fetchall()
        return [_row_to_referral_medication(r) for r in rows]

    def update_referral_medication(
        self,
        scope: Scope,
        referral_id: int,
        medication_id: int,
        *,
        name: str | None = None,
        dose: str | None = None,
        route: str | None = None,
        frequency: str | None = None,
        source: str | None = None,
    ) -> ReferralMedication | None:
        if source is not None and source not in SOURCE_VALUES:
            raise ValueError(f"Unknown source: {source!r}")
        if self.get_referral(scope, referral_id) is None:
            return None
        fields: dict[str, Any] = {
            k: v
            for k, v in {
                "name": name,
                "dose": dose,
                "route": route,
                "frequency": frequency,
                "source": source,
            }.items()
            if v is not None
        }
        if not fields:
            row = self._conn.execute(
                "SELECT * FROM referral_medications WHERE id = ? AND referral_id = ?",
                (medication_id, referral_id),
            ).fetchone()
            return _row_to_referral_medication(row) if row else None
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        cursor = self._conn.execute(
            f"UPDATE referral_medications SET {set_clause} WHERE id = ? AND referral_id = ?",
            [*fields.values(), medication_id, referral_id],
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        row = self._conn.execute(
            "SELECT * FROM referral_medications WHERE id = ?", (medication_id,)
        ).fetchone()
        return _row_to_referral_medication(row) if row else None

    def delete_referral_medication(
        self, scope: Scope, referral_id: int, medication_id: int
    ) -> bool:
        if self.get_referral(scope, referral_id) is None:
            return False
        cursor = self._conn.execute(
            "DELETE FROM referral_medications WHERE id = ? AND referral_id = ?",
            (medication_id, referral_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # --- Allergies ---

    def add_referral_allergy(
        self,
        scope: Scope,
        referral_id: int,
        *,
        substance: str,
        reaction: str | None = None,
        severity: str | None = None,
        source: str = "user_entered",
    ) -> ReferralAllergy | None:
        if source not in SOURCE_VALUES:
            raise ValueError(f"Unknown source: {source!r}")
        if self.get_referral(scope, referral_id) is None:
            return None
        cursor = self._conn.execute(
            "INSERT INTO referral_allergies "
            "(referral_id, substance, reaction, severity, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (referral_id, substance, reaction, severity, source),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM referral_allergies WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_referral_allergy(row)

    def list_referral_allergies(self, scope: Scope, referral_id: int) -> list[ReferralAllergy]:
        if self.get_referral(scope, referral_id) is None:
            return []
        rows = self._conn.execute(
            "SELECT * FROM referral_allergies WHERE referral_id = ? ORDER BY id ASC",
            (referral_id,),
        ).fetchall()
        return [_row_to_referral_allergy(r) for r in rows]

    def update_referral_allergy(
        self,
        scope: Scope,
        referral_id: int,
        allergy_id: int,
        *,
        substance: str | None = None,
        reaction: str | None = None,
        severity: str | None = None,
        source: str | None = None,
    ) -> ReferralAllergy | None:
        if source is not None and source not in SOURCE_VALUES:
            raise ValueError(f"Unknown source: {source!r}")
        if self.get_referral(scope, referral_id) is None:
            return None
        fields: dict[str, Any] = {
            k: v
            for k, v in {
                "substance": substance,
                "reaction": reaction,
                "severity": severity,
                "source": source,
            }.items()
            if v is not None
        }
        if not fields:
            row = self._conn.execute(
                "SELECT * FROM referral_allergies WHERE id = ? AND referral_id = ?",
                (allergy_id, referral_id),
            ).fetchone()
            return _row_to_referral_allergy(row) if row else None
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        cursor = self._conn.execute(
            f"UPDATE referral_allergies SET {set_clause} WHERE id = ? AND referral_id = ?",
            [*fields.values(), allergy_id, referral_id],
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        row = self._conn.execute(
            "SELECT * FROM referral_allergies WHERE id = ?", (allergy_id,)
        ).fetchone()
        return _row_to_referral_allergy(row) if row else None

    def delete_referral_allergy(self, scope: Scope, referral_id: int, allergy_id: int) -> bool:
        if self.get_referral(scope, referral_id) is None:
            return False
        cursor = self._conn.execute(
            "DELETE FROM referral_allergies WHERE id = ? AND referral_id = ?",
            (allergy_id, referral_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # --- Attachments ---

    def add_referral_attachment(
        self,
        scope: Scope,
        referral_id: int,
        *,
        kind: str,
        label: str,
        date_of_service: str | None = None,
        storage_ref: str | None = None,
        checklist_only: bool = True,
        source: str = "user_entered",
    ) -> ReferralAttachment | None:
        if kind not in ATTACHMENT_KIND_VALUES:
            raise ValueError(f"Unknown attachment kind: {kind!r}")
        if source not in SOURCE_VALUES:
            raise ValueError(f"Unknown source: {source!r}")
        if self.get_referral(scope, referral_id) is None:
            return None
        cursor = self._conn.execute(
            "INSERT INTO referral_attachments "
            "(referral_id, kind, label, date_of_service, storage_ref, checklist_only, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                referral_id,
                kind,
                label,
                date_of_service,
                storage_ref,
                1 if checklist_only else 0,
                source,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM referral_attachments WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_referral_attachment(row)

    def list_referral_attachments(self, scope: Scope, referral_id: int) -> list[ReferralAttachment]:
        if self.get_referral(scope, referral_id) is None:
            return []
        rows = self._conn.execute(
            "SELECT * FROM referral_attachments WHERE referral_id = ? ORDER BY id ASC",
            (referral_id,),
        ).fetchall()
        return [_row_to_referral_attachment(r) for r in rows]

    def update_referral_attachment(
        self,
        scope: Scope,
        referral_id: int,
        attachment_id: int,
        *,
        kind: str | None = None,
        label: str | None = None,
        date_of_service: str | None = None,
        storage_ref: str | None = None,
        checklist_only: bool | None = None,
        source: str | None = None,
    ) -> ReferralAttachment | None:
        if kind is not None and kind not in ATTACHMENT_KIND_VALUES:
            raise ValueError(f"Unknown attachment kind: {kind!r}")
        if source is not None and source not in SOURCE_VALUES:
            raise ValueError(f"Unknown source: {source!r}")
        if self.get_referral(scope, referral_id) is None:
            return None
        fields: dict[str, Any] = {}
        if kind is not None:
            fields["kind"] = kind
        if label is not None:
            fields["label"] = label
        if date_of_service is not None:
            fields["date_of_service"] = date_of_service
        if storage_ref is not None:
            fields["storage_ref"] = storage_ref
        if checklist_only is not None:
            fields["checklist_only"] = 1 if checklist_only else 0
        if source is not None:
            fields["source"] = source
        if not fields:
            row = self._conn.execute(
                "SELECT * FROM referral_attachments WHERE id = ? AND referral_id = ?",
                (attachment_id, referral_id),
            ).fetchone()
            return _row_to_referral_attachment(row) if row else None
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        cursor = self._conn.execute(
            f"UPDATE referral_attachments SET {set_clause} WHERE id = ? AND referral_id = ?",
            [*fields.values(), attachment_id, referral_id],
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        row = self._conn.execute(
            "SELECT * FROM referral_attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
        return _row_to_referral_attachment(row) if row else None

    def delete_referral_attachment(
        self, scope: Scope, referral_id: int, attachment_id: int
    ) -> bool:
        if self.get_referral(scope, referral_id) is None:
            return False
        cursor = self._conn.execute(
            "DELETE FROM referral_attachments WHERE id = ? AND referral_id = ?",
            (attachment_id, referral_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # --- Referral responses (closed-loop) ---

    def record_referral_response(
        self,
        scope: Scope,
        referral_id: int,
        *,
        appointment_date: str | None = None,
        consult_completed: bool = False,
        recommendations_text: str | None = None,
        attached_consult_note_ref: str | None = None,
        received_via: str = "manual",
        recorded_by_user_id: int | None = None,
    ) -> ReferralResponse | None:
        if received_via not in RECEIVED_VIA_VALUES:
            raise ValueError(f"Unknown received_via: {received_via!r}")
        if self.get_referral(scope, referral_id) is None:
            return None
        cursor = self._conn.execute(
            "INSERT INTO referral_responses "
            "(referral_id, appointment_date, consult_completed, recommendations_text, "
            "attached_consult_note_ref, received_via, recorded_by_user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                referral_id,
                appointment_date,
                1 if consult_completed else 0,
                recommendations_text,
                attached_consult_note_ref,
                received_via,
                recorded_by_user_id,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM referral_responses WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_referral_response(row)

    def list_referral_responses(self, scope: Scope, referral_id: int) -> list[ReferralResponse]:
        if self.get_referral(scope, referral_id) is None:
            return []
        rows = self._conn.execute(
            "SELECT * FROM referral_responses WHERE referral_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (referral_id,),
        ).fetchall()
        return [_row_to_referral_response(r) for r in rows]

    def update_referral_response(
        self,
        scope: Scope,
        referral_id: int,
        response_id: int,
        *,
        appointment_date: str | None = None,
        consult_completed: bool | None = None,
        recommendations_text: str | None = None,
        attached_consult_note_ref: str | None = None,
        received_via: str | None = None,
    ) -> ReferralResponse | None:
        if received_via is not None and received_via not in RECEIVED_VIA_VALUES:
            raise ValueError(f"Unknown received_via: {received_via!r}")
        if self.get_referral(scope, referral_id) is None:
            return None
        fields: dict[str, Any] = {}
        if appointment_date is not None:
            fields["appointment_date"] = appointment_date
        if consult_completed is not None:
            fields["consult_completed"] = 1 if consult_completed else 0
        if recommendations_text is not None:
            fields["recommendations_text"] = recommendations_text
        if attached_consult_note_ref is not None:
            fields["attached_consult_note_ref"] = attached_consult_note_ref
        if received_via is not None:
            fields["received_via"] = received_via
        if not fields:
            row = self._conn.execute(
                "SELECT * FROM referral_responses WHERE id = ? AND referral_id = ?",
                (response_id, referral_id),
            ).fetchone()
            return _row_to_referral_response(row) if row else None
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        cursor = self._conn.execute(
            f"UPDATE referral_responses SET {set_clause}, updated_at = datetime('now') "
            "WHERE id = ? AND referral_id = ?",
            [*fields.values(), response_id, referral_id],
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        row = self._conn.execute(
            "SELECT * FROM referral_responses WHERE id = ?", (response_id,)
        ).fetchone()
        return _row_to_referral_response(row) if row else None

    def delete_referral_response(self, scope: Scope, referral_id: int, response_id: int) -> bool:
        if self.get_referral(scope, referral_id) is None:
            return False
        cursor = self._conn.execute(
            "DELETE FROM referral_responses WHERE id = ? AND referral_id = ?",
            (response_id, referral_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # --- Insurance plans (scope-owned) ---

    def create_insurance_plan(
        self,
        scope: Scope,
        *,
        payer_name: str,
        plan_name: str | None = None,
        plan_type: str = "other",
        member_id_pattern: str | None = None,
        group_id_pattern: str | None = None,
        requires_referral: bool = False,
        requires_prior_auth: bool = False,
        notes: str | None = None,
    ) -> InsurancePlan:
        scope_sql_clause(scope)  # raises on anonymous
        if plan_type not in PLAN_TYPE_VALUES:
            raise ValueError(f"Unknown plan_type: {plan_type!r}")
        # payer_name is embedded into payer_key as "{payer_name}|{plan_type}"
        # by the rules engine — reject pipe characters so derived keys remain
        # unambiguous. No real US payer name contains this character.
        if "|" in payer_name:
            raise ValueError("payer_name must not contain the '|' character")
        cursor = self._conn.execute(
            """INSERT INTO insurance_plans
               (scope_user_id, scope_organization_id, payer_name, plan_name, plan_type,
                member_id_pattern, group_id_pattern, requires_referral, requires_prior_auth,
                notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                scope.user_id if scope.is_solo else None,
                scope.organization_id if scope.is_org else None,
                payer_name,
                plan_name,
                plan_type,
                member_id_pattern,
                group_id_pattern,
                1 if requires_referral else 0,
                1 if requires_prior_auth else 0,
                notes,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM insurance_plans WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_insurance_plan(row)

    def get_insurance_plan(self, scope: Scope, plan_id: int) -> InsurancePlan | None:
        clause, params = scope_sql_clause(scope)
        row = self._conn.execute(
            f"SELECT * FROM insurance_plans WHERE id = ? AND {clause} AND deleted_at IS NULL",
            [plan_id, *params],
        ).fetchone()
        return _row_to_insurance_plan(row) if row else None

    def list_insurance_plans(
        self, scope: Scope, *, include_deleted: bool = False
    ) -> list[InsurancePlan]:
        clause, params = scope_sql_clause(scope)
        where = [clause]
        if not include_deleted:
            where.append("deleted_at IS NULL")
        rows = self._conn.execute(
            f"SELECT * FROM insurance_plans WHERE {' AND '.join(where)} "
            "ORDER BY payer_name ASC, plan_name ASC, id ASC",
            params,
        ).fetchall()
        return [_row_to_insurance_plan(r) for r in rows]

    def update_insurance_plan(
        self,
        scope: Scope,
        plan_id: int,
        *,
        payer_name: str | None = None,
        plan_name: str | None = None,
        plan_type: str | None = None,
        member_id_pattern: str | None = None,
        group_id_pattern: str | None = None,
        requires_referral: bool | None = None,
        requires_prior_auth: bool | None = None,
        notes: str | None = None,
    ) -> InsurancePlan | None:
        if plan_type is not None and plan_type not in PLAN_TYPE_VALUES:
            raise ValueError(f"Unknown plan_type: {plan_type!r}")
        fields: dict[str, Any] = {}
        if payer_name is not None:
            fields["payer_name"] = payer_name
        if plan_name is not None:
            fields["plan_name"] = plan_name
        if plan_type is not None:
            fields["plan_type"] = plan_type
        if member_id_pattern is not None:
            fields["member_id_pattern"] = member_id_pattern
        if group_id_pattern is not None:
            fields["group_id_pattern"] = group_id_pattern
        if requires_referral is not None:
            fields["requires_referral"] = 1 if requires_referral else 0
        if requires_prior_auth is not None:
            fields["requires_prior_auth"] = 1 if requires_prior_auth else 0
        if notes is not None:
            fields["notes"] = notes
        if not fields:
            return self.get_insurance_plan(scope, plan_id)
        clause, scope_params = scope_sql_clause(scope)
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        cursor = self._conn.execute(
            f"UPDATE insurance_plans SET {set_clause}, updated_at = datetime('now') "
            f"WHERE id = ? AND {clause} AND deleted_at IS NULL",
            [*fields.values(), plan_id, *scope_params],
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_insurance_plan(scope, plan_id)

    def soft_delete_insurance_plan(self, scope: Scope, plan_id: int) -> bool:
        clause, params = scope_sql_clause(scope)
        cursor = self._conn.execute(
            f"UPDATE insurance_plans SET deleted_at = datetime('now') "
            f"WHERE id = ? AND {clause} AND deleted_at IS NULL",
            [plan_id, *params],
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # --- Specialty rules (platform default or org override) ---

    def create_specialty_rule(
        self,
        *,
        specialty_code: str,
        organization_id: int | None = None,
        display_name: str | None = None,
        required_fields: dict[str, Any] | None = None,
        recommended_attachments: dict[str, Any] | None = None,
        intake_questions: dict[str, Any] | None = None,
        urgency_red_flags: dict[str, Any] | None = None,
        common_rejection_reasons: dict[str, Any] | None = None,
        source: str = "seed",
    ) -> SpecialtyRule:
        if source not in RULE_SOURCE_VALUES:
            raise ValueError(f"Unknown source: {source!r}")
        cursor = self._conn.execute(
            """INSERT INTO specialty_rules
               (organization_id, specialty_code, display_name,
                required_fields, recommended_attachments, intake_questions,
                urgency_red_flags, common_rejection_reasons, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                organization_id,
                specialty_code,
                display_name,
                json.dumps(required_fields or {}),
                json.dumps(recommended_attachments or {}),
                json.dumps(intake_questions or {}),
                json.dumps(urgency_red_flags or {}),
                json.dumps(common_rejection_reasons or {}),
                source,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM specialty_rules WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_specialty_rule(row)

    def get_specialty_rule(self, rule_id: int) -> SpecialtyRule | None:
        row = self._conn.execute(
            "SELECT * FROM specialty_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        return _row_to_specialty_rule(row) if row else None

    def list_specialty_rules(
        self,
        *,
        organization_id: int | None = None,
        include_globals: bool = True,
        specialty_code: str | None = None,
    ) -> list[SpecialtyRule]:
        code_clause = " AND specialty_code = ?" if specialty_code is not None else ""
        code_params: tuple = (specialty_code,) if specialty_code is not None else ()
        if organization_id is None:
            # Just globals.
            rows = self._conn.execute(
                "SELECT * FROM specialty_rules WHERE organization_id IS NULL"
                + code_clause
                + " ORDER BY specialty_code ASC, id ASC",
                code_params,
            ).fetchall()
        elif include_globals:
            rows = self._conn.execute(
                "SELECT * FROM specialty_rules "
                "WHERE (organization_id IS NULL OR organization_id = ?)"
                + code_clause
                + " ORDER BY specialty_code ASC, organization_id NULLS FIRST, id ASC",
                (organization_id, *code_params),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM specialty_rules WHERE organization_id = ?"
                + code_clause
                + " ORDER BY specialty_code ASC, id ASC",
                (organization_id, *code_params),
            ).fetchall()
        return [_row_to_specialty_rule(r) for r in rows]

    def update_specialty_rule(
        self,
        rule_id: int,
        *,
        display_name: str | None = None,
        required_fields: dict[str, Any] | None = None,
        recommended_attachments: dict[str, Any] | None = None,
        intake_questions: dict[str, Any] | None = None,
        urgency_red_flags: dict[str, Any] | None = None,
        common_rejection_reasons: dict[str, Any] | None = None,
        source: str | None = None,
        bump_version: bool = True,
        overwrite: bool = False,
    ) -> SpecialtyRule | None:
        if source is not None and source not in RULE_SOURCE_VALUES:
            raise ValueError(f"Unknown source: {source!r}")
        fields: dict[str, Any] = {}
        if overwrite:
            # Write every passed kwarg literally, including None. Keeps seed
            # re-runs able to restore a column to NULL even if an admin had
            # edited it. But the JSONB columns are NOT NULL in both schemas
            # (see migration 010 + storage.py DDL), so refuse None eagerly
            # with a clear error — silently writing it would hit a DB
            # constraint mid-transaction and give a much worse message.
            for col_name, col_val in (
                ("required_fields", required_fields),
                ("recommended_attachments", recommended_attachments),
                ("intake_questions", intake_questions),
                ("urgency_red_flags", urgency_red_flags),
                ("common_rejection_reasons", common_rejection_reasons),
            ):
                if col_val is None:
                    raise ValueError(
                        f"overwrite=True requires a non-None value for {col_name!r} "
                        "(column is NOT NULL in the schema)"
                    )
            fields["display_name"] = display_name
            fields["required_fields"] = json.dumps(required_fields)
            fields["recommended_attachments"] = json.dumps(recommended_attachments)
            fields["intake_questions"] = json.dumps(intake_questions)
            fields["urgency_red_flags"] = json.dumps(urgency_red_flags)
            fields["common_rejection_reasons"] = json.dumps(common_rejection_reasons)
            if source is not None:
                fields["source"] = source
        else:
            if display_name is not None:
                fields["display_name"] = display_name
            if required_fields is not None:
                fields["required_fields"] = json.dumps(required_fields)
            if recommended_attachments is not None:
                fields["recommended_attachments"] = json.dumps(recommended_attachments)
            if intake_questions is not None:
                fields["intake_questions"] = json.dumps(intake_questions)
            if urgency_red_flags is not None:
                fields["urgency_red_flags"] = json.dumps(urgency_red_flags)
            if common_rejection_reasons is not None:
                fields["common_rejection_reasons"] = json.dumps(common_rejection_reasons)
            if source is not None:
                fields["source"] = source
        if not fields:
            return self.get_specialty_rule(rule_id)
        set_parts = [f"{k} = ?" for k in fields]
        params: list[Any] = list(fields.values())
        if bump_version:
            set_parts.append("version_id = version_id + 1")
        set_parts.append("updated_at = datetime('now')")
        params.append(rule_id)
        cursor = self._conn.execute(
            f"UPDATE specialty_rules SET {', '.join(set_parts)} WHERE id = ?",
            params,
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_specialty_rule(rule_id)

    def delete_specialty_rule(self, rule_id: int) -> bool:
        cursor = self._conn.execute("DELETE FROM specialty_rules WHERE id = ?", (rule_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    # --- Payer rules (platform default or org override) ---

    def create_payer_rule(
        self,
        *,
        payer_key: str,
        organization_id: int | None = None,
        display_name: str | None = None,
        referral_required: bool = False,
        auth_required_services: dict[str, Any] | None = None,
        auth_typical_turnaround_days: int | None = None,
        records_required: dict[str, Any] | None = None,
        notes: str | None = None,
        source: str = "seed",
    ) -> PayerRule:
        if source not in RULE_SOURCE_VALUES:
            raise ValueError(f"Unknown source: {source!r}")
        cursor = self._conn.execute(
            """INSERT INTO payer_rules
               (organization_id, payer_key, display_name, referral_required,
                auth_required_services, auth_typical_turnaround_days,
                records_required, notes, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                organization_id,
                payer_key,
                display_name,
                1 if referral_required else 0,
                json.dumps(auth_required_services or {}),
                auth_typical_turnaround_days,
                json.dumps(records_required or {}),
                notes,
                source,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM payer_rules WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_payer_rule(row)

    def get_payer_rule(self, rule_id: int) -> PayerRule | None:
        row = self._conn.execute("SELECT * FROM payer_rules WHERE id = ?", (rule_id,)).fetchone()
        return _row_to_payer_rule(row) if row else None

    def list_payer_rules(
        self,
        *,
        organization_id: int | None = None,
        include_globals: bool = True,
        payer_key: str | None = None,
    ) -> list[PayerRule]:
        key_clause = " AND payer_key = ?" if payer_key is not None else ""
        key_params: tuple = (payer_key,) if payer_key is not None else ()
        if organization_id is None:
            rows = self._conn.execute(
                "SELECT * FROM payer_rules WHERE organization_id IS NULL"
                + key_clause
                + " ORDER BY payer_key ASC, id ASC",
                key_params,
            ).fetchall()
        elif include_globals:
            rows = self._conn.execute(
                "SELECT * FROM payer_rules "
                "WHERE (organization_id IS NULL OR organization_id = ?)"
                + key_clause
                + " ORDER BY payer_key ASC, organization_id NULLS FIRST, id ASC",
                (organization_id, *key_params),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM payer_rules WHERE organization_id = ?"
                + key_clause
                + " ORDER BY payer_key ASC, id ASC",
                (organization_id, *key_params),
            ).fetchall()
        return [_row_to_payer_rule(r) for r in rows]

    def update_payer_rule(
        self,
        rule_id: int,
        *,
        display_name: str | None = None,
        referral_required: bool | None = None,
        auth_required_services: dict[str, Any] | None = None,
        auth_typical_turnaround_days: int | None = None,
        records_required: dict[str, Any] | None = None,
        notes: str | None = None,
        source: str | None = None,
        bump_version: bool = True,
        overwrite: bool = False,
    ) -> PayerRule | None:
        if source is not None and source not in RULE_SOURCE_VALUES:
            raise ValueError(f"Unknown source: {source!r}")
        fields: dict[str, Any] = {}
        if overwrite:
            # ``referral_required``, ``auth_required_services``, ``records_required``
            # are NOT NULL in the schema. Refuse None eagerly with a clear error.
            for col_name, col_val in (
                ("referral_required", referral_required),
                ("auth_required_services", auth_required_services),
                ("records_required", records_required),
            ):
                if col_val is None:
                    raise ValueError(
                        f"overwrite=True requires a non-None value for {col_name!r} "
                        "(column is NOT NULL in the schema)"
                    )
            fields["display_name"] = display_name
            fields["referral_required"] = 1 if referral_required else 0
            fields["auth_required_services"] = json.dumps(auth_required_services)
            # ``auth_typical_turnaround_days`` is nullable — None is legitimate
            # (e.g. Medicare). ``notes`` is nullable too.
            fields["auth_typical_turnaround_days"] = auth_typical_turnaround_days
            fields["records_required"] = json.dumps(records_required)
            fields["notes"] = notes
            if source is not None:
                fields["source"] = source
        else:
            if display_name is not None:
                fields["display_name"] = display_name
            if referral_required is not None:
                fields["referral_required"] = 1 if referral_required else 0
            if auth_required_services is not None:
                fields["auth_required_services"] = json.dumps(auth_required_services)
            if auth_typical_turnaround_days is not None:
                fields["auth_typical_turnaround_days"] = auth_typical_turnaround_days
            if records_required is not None:
                fields["records_required"] = json.dumps(records_required)
            if notes is not None:
                fields["notes"] = notes
            if source is not None:
                fields["source"] = source
        if not fields:
            return self.get_payer_rule(rule_id)
        set_parts = [f"{k} = ?" for k in fields]
        params: list[Any] = list(fields.values())
        if bump_version:
            set_parts.append("version_id = version_id + 1")
        set_parts.append("updated_at = datetime('now')")
        params.append(rule_id)
        cursor = self._conn.execute(
            f"UPDATE payer_rules SET {', '.join(set_parts)} WHERE id = ?",
            params,
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_payer_rule(rule_id)

    def delete_payer_rule(self, rule_id: int) -> bool:
        cursor = self._conn.execute("DELETE FROM payer_rules WHERE id = ?", (rule_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    # --- CSV imports (scope-owned) + import rows (scope-transitive) ---

    def create_csv_import(
        self,
        scope: Scope,
        *,
        original_filename: str,
        uploaded_by_user_id: int | None = None,
        row_count: int = 0,
        mapping: dict[str, Any] | None = None,
    ) -> CsvImport:
        scope_sql_clause(scope)  # raises on anonymous
        cursor = self._conn.execute(
            """INSERT INTO csv_imports
               (scope_user_id, scope_organization_id, uploaded_by_user_id,
                original_filename, row_count, mapping)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                scope.user_id if scope.is_solo else None,
                scope.organization_id if scope.is_org else None,
                uploaded_by_user_id,
                original_filename,
                row_count,
                json.dumps(mapping or {}),
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM csv_imports WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_csv_import(row)

    def get_csv_import(self, scope: Scope, import_id: int) -> CsvImport | None:
        clause, params = scope_sql_clause(scope)
        row = self._conn.execute(
            f"SELECT * FROM csv_imports WHERE id = ? AND {clause}",
            [import_id, *params],
        ).fetchone()
        return _row_to_csv_import(row) if row else None

    def list_csv_imports(
        self, scope: Scope, *, limit: int = 50, offset: int = 0
    ) -> list[CsvImport]:
        clause, params = scope_sql_clause(scope)
        rows = self._conn.execute(
            f"SELECT * FROM csv_imports WHERE {clause} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        ).fetchall()
        return [_row_to_csv_import(r) for r in rows]

    def update_csv_import(
        self,
        scope: Scope,
        import_id: int,
        *,
        status: str | None = None,
        row_count: int | None = None,
        mapping: dict[str, Any] | None = None,
        error_report: dict[str, Any] | None = None,
    ) -> CsvImport | None:
        if status is not None and status not in IMPORT_STATUS_VALUES:
            raise ValueError(f"Unknown import status: {status!r}")
        fields: dict[str, Any] = {}
        if status is not None:
            fields["status"] = status
        if row_count is not None:
            fields["row_count"] = row_count
        if mapping is not None:
            fields["mapping"] = json.dumps(mapping)
        if error_report is not None:
            fields["error_report"] = json.dumps(error_report)
        if not fields:
            return self.get_csv_import(scope, import_id)
        clause, scope_params = scope_sql_clause(scope)
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        cursor = self._conn.execute(
            f"UPDATE csv_imports SET {set_clause}, updated_at = datetime('now') "
            f"WHERE id = ? AND {clause}",
            [*fields.values(), import_id, *scope_params],
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_csv_import(scope, import_id)

    def delete_csv_import(self, scope: Scope, import_id: int) -> bool:
        # Hard delete — an import is ephemeral staging data. Audit log
        # captures the batch_committed / batch_failed event separately.
        clause, params = scope_sql_clause(scope)
        cursor = self._conn.execute(
            f"DELETE FROM csv_imports WHERE id = ? AND {clause}",
            [import_id, *params],
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def add_csv_import_row(
        self,
        scope: Scope,
        import_id: int,
        *,
        row_index: int,
        raw_json: dict[str, Any] | None = None,
        validation_errors: dict[str, Any] | None = None,
        status: str = "pending",
    ) -> CsvImportRow | None:
        if status not in IMPORT_ROW_STATUS_VALUES:
            raise ValueError(f"Unknown row status: {status!r}")
        if self.get_csv_import(scope, import_id) is None:
            return None
        cursor = self._conn.execute(
            """INSERT INTO csv_import_rows
               (import_id, row_index, raw_json, validation_errors, status)
               VALUES (?, ?, ?, ?, ?)""",
            (
                import_id,
                row_index,
                json.dumps(raw_json or {}),
                json.dumps(validation_errors or {}),
                status,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM csv_import_rows WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_csv_import_row(row)

    def list_csv_import_rows(
        self,
        scope: Scope,
        import_id: int,
        *,
        status: str | None = None,
        limit: int = 2000,
        offset: int = 0,
    ) -> list[CsvImportRow]:
        if self.get_csv_import(scope, import_id) is None:
            return []
        where = ["import_id = ?"]
        params: list[Any] = [import_id]
        if status is not None:
            where.append("status = ?")
            params.append(status)
        params.extend([int(limit), int(offset)])
        rows = self._conn.execute(
            f"SELECT * FROM csv_import_rows WHERE {' AND '.join(where)} "
            "ORDER BY row_index ASC, id ASC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [_row_to_csv_import_row(r) for r in rows]

    def update_csv_import_row(
        self,
        scope: Scope,
        import_id: int,
        row_id: int,
        *,
        raw_json: dict[str, Any] | None = None,
        validation_errors: dict[str, Any] | None = None,
        status: str | None = None,
        referral_id: int | None = None,
    ) -> CsvImportRow | None:
        if status is not None and status not in IMPORT_ROW_STATUS_VALUES:
            raise ValueError(f"Unknown row status: {status!r}")
        if self.get_csv_import(scope, import_id) is None:
            return None
        if referral_id is not None and self.get_referral(scope, referral_id) is None:
            raise ValueError(f"referral_id={referral_id} not accessible from the caller's scope")
        fields: dict[str, Any] = {}
        if raw_json is not None:
            fields["raw_json"] = json.dumps(raw_json)
        if validation_errors is not None:
            fields["validation_errors"] = json.dumps(validation_errors)
        if status is not None:
            fields["status"] = status
        if referral_id is not None:
            fields["referral_id"] = referral_id
        if not fields:
            row = self._conn.execute(
                "SELECT * FROM csv_import_rows WHERE id = ? AND import_id = ?",
                (row_id, import_id),
            ).fetchone()
            return _row_to_csv_import_row(row) if row else None
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        cursor = self._conn.execute(
            f"UPDATE csv_import_rows SET {set_clause}, updated_at = datetime('now') "
            "WHERE id = ? AND import_id = ?",
            [*fields.values(), row_id, import_id],
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        row = self._conn.execute("SELECT * FROM csv_import_rows WHERE id = ?", (row_id,)).fetchone()
        return _row_to_csv_import_row(row) if row else None

    def delete_csv_import_row(self, scope: Scope, import_id: int, row_id: int) -> bool:
        if self.get_csv_import(scope, import_id) is None:
            return False
        cursor = self._conn.execute(
            "DELETE FROM csv_import_rows WHERE id = ? AND import_id = ?",
            (row_id, import_id),
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
