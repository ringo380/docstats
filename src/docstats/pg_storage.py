"""Supabase Postgres storage backend.

Used in production when SUPABASE_URL + SUPABASE_SERVICE_KEY env vars are set.
Tables are prefixed with ``docstats_`` to coexist with other apps in the same
Supabase project.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from docstats.domain.audit import AuditEvent
from docstats.domain.orgs import ROLES, Membership, Organization
from docstats.domain.patients import Patient
from docstats.domain.sessions import Session
from docstats.models import NPIResult, SavedProvider, SearchHistoryEntry
from docstats.scope import Scope, ScopeRequired
from docstats.storage_base import StorageBase, fuzzy_score, normalize_email
from docstats.validators import IP_MAX_LENGTH, USER_AGENT_MAX_LENGTH

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO timestamp string from Supabase into a datetime."""
    if not value:
        return None
    return datetime.fromisoformat(value)


def _row_to_organization(row: dict) -> Organization:
    """Convert a Supabase organizations row into an Organization model."""
    created_at = _parse_ts(row.get("created_at"))
    assert created_at is not None
    return Organization(
        id=int(row["id"]),
        name=row["name"],
        slug=row["slug"],
        npi=row.get("npi"),
        address_line1=row.get("address_line1"),
        address_line2=row.get("address_line2"),
        address_city=row.get("address_city"),
        address_state=row.get("address_state"),
        address_zip=row.get("address_zip"),
        phone=row.get("phone"),
        fax=row.get("fax"),
        terms_bundle_version=row.get("terms_bundle_version"),
        created_at=created_at,
        deleted_at=_parse_ts(row.get("deleted_at")),
    )


def _row_to_membership(row: dict) -> Membership:
    """Convert a Supabase memberships row into a Membership model."""
    joined_at = _parse_ts(row.get("joined_at"))
    assert joined_at is not None
    return Membership(
        id=int(row["id"]),
        organization_id=int(row["organization_id"]),
        user_id=int(row["user_id"]),
        role=row["role"],
        invited_by_user_id=row.get("invited_by_user_id"),
        joined_at=joined_at,
        deleted_at=_parse_ts(row.get("deleted_at")),
    )


def _row_to_patient(row: dict) -> Patient:
    """Convert a Supabase patients row into a Patient model."""
    created = _parse_ts(row.get("created_at"))
    updated = _parse_ts(row.get("updated_at"))
    assert created is not None and updated is not None
    # Postgres DATE comes back as ISO string (YYYY-MM-DD) via PostgREST.
    dob = row.get("date_of_birth")
    return Patient(
        id=int(row["id"]),
        scope_user_id=row.get("scope_user_id"),
        scope_organization_id=row.get("scope_organization_id"),
        first_name=row["first_name"],
        last_name=row["last_name"],
        middle_name=row.get("middle_name"),
        date_of_birth=dob,
        sex=row.get("sex"),
        mrn=row.get("mrn"),
        preferred_language=row.get("preferred_language"),
        pronouns=row.get("pronouns"),
        phone=row.get("phone"),
        email=row.get("email"),
        address_line1=row.get("address_line1"),
        address_line2=row.get("address_line2"),
        address_city=row.get("address_city"),
        address_state=row.get("address_state"),
        address_zip=row.get("address_zip"),
        emergency_contact_name=row.get("emergency_contact_name"),
        emergency_contact_phone=row.get("emergency_contact_phone"),
        notes=row.get("notes"),
        created_by_user_id=row.get("created_by_user_id"),
        created_at=created,
        updated_at=updated,
        deleted_at=_parse_ts(row.get("deleted_at")),
    )


def _row_to_session(row: dict) -> Session:
    """Convert a Supabase sessions row into a Session model."""
    data = row.get("data")
    if isinstance(data, str):
        data = json.loads(data) if data else {}
    elif data is None:
        data = {}
    created = _parse_ts(row.get("created_at"))
    last_seen = _parse_ts(row.get("last_seen_at"))
    expires = _parse_ts(row.get("expires_at"))
    assert created is not None and last_seen is not None and expires is not None
    return Session(
        id=row["id"],
        user_id=row.get("user_id"),
        data=data,
        ip=row.get("ip"),
        user_agent=row.get("user_agent"),
        created_at=created,
        last_seen_at=last_seen,
        expires_at=expires,
        revoked_at=_parse_ts(row.get("revoked_at")),
    )


def _row_to_audit_event(row: dict) -> AuditEvent:
    """Convert a Supabase audit_events row into an AuditEvent."""
    # Supabase JSONB returns dicts already; tolerate string too (some SDK paths).
    meta = row.get("metadata")
    if isinstance(meta, str):
        meta = json.loads(meta) if meta else {}
    elif meta is None:
        meta = {}
    created_at = _parse_ts(row.get("created_at"))
    assert created_at is not None  # DEFAULT now() guarantees present
    return AuditEvent(
        id=int(row["id"]),
        actor_user_id=row.get("actor_user_id"),
        scope_user_id=row.get("scope_user_id"),
        scope_organization_id=row.get("scope_organization_id"),
        action=row["action"],
        entity_type=row.get("entity_type"),
        entity_id=row.get("entity_id"),
        metadata=meta,
        ip=row.get("ip"),
        user_agent=row.get("user_agent"),
        created_at=created_at,
    )


class PostgresStorage(StorageBase):
    """Supabase-backed storage using the supabase-py REST client."""

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
    def _row_to_provider(row: dict) -> SavedProvider:
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
            appt_suite=row.get("appt_suite"),
            appt_phone=row.get("appt_phone"),
            appt_fax=row.get("appt_fax"),
            is_televisit=bool(row.get("is_televisit", False)),
            enrichment_json=row.get("enrichment_json"),
            saved_at=_parse_ts(row.get("saved_at")),
            updated_at=_parse_ts(row.get("updated_at")),
        )

    # --- User CRUD ---

    def create_user(self, email: str, password_hash: str) -> int:
        result = (
            self._t("users")
            .insert({"email": normalize_email(email), "password_hash": password_hash})
            .execute()
        )
        return int(result.data[0]["id"])

    def get_user_by_id(self, user_id: int) -> dict | None:
        result = self._t("users").select("*").eq("id", user_id).execute()
        return result.data[0] if result.data else None

    def get_user_by_email(self, email: str) -> dict | None:
        result = self._t("users").select("*").eq("email", normalize_email(email)).execute()
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
        now = _now_iso()
        existing = self.get_user_by_github_id(github_id)
        if existing:
            updates: dict = {"github_login": github_login, "last_login_at": now}
            if display_name is not None:
                updates["display_name"] = display_name
            self._t("users").update(updates).eq("id", existing["id"]).execute()
            return int(existing["id"])
        if email:
            existing_email = self.get_user_by_email(email)
            if existing_email:
                self._t("users").update(
                    {"github_id": github_id, "github_login": github_login, "last_login_at": now}
                ).eq("id", existing_email["id"]).execute()
                return int(existing_email["id"])
        safe_email = normalize_email(email) if email else f"github_{github_id}@noemail.invalid"
        result = (
            self._t("users")
            .upsert(
                {
                    "email": safe_email,
                    "github_id": github_id,
                    "github_login": github_login,
                    "display_name": display_name,
                },
                on_conflict="github_id",
            )
            .execute()
        )
        return int(result.data[0]["id"])

    def update_last_login(self, user_id: int) -> None:
        self._t("users").update({"last_login_at": _now_iso()}).eq("id", user_id).execute()

    def set_user_pcp(self, user_id: int, pcp_npi: str) -> None:
        self._t("users").update({"pcp_npi": pcp_npi}).eq("id", user_id).execute()

    def clear_user_pcp(self, user_id: int) -> None:
        self._t("users").update({"pcp_npi": None}).eq("id", user_id).execute()

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
        self._t("users").update(fields).eq("id", user_id).execute()

    def record_terms_acceptance(
        self,
        user_id: int,
        *,
        terms_version: str,
        ip_address: str,
        user_agent: str,
    ) -> None:
        self._t("users").update(
            {
                "terms_accepted_at": _now_iso(),
                "terms_version": terms_version,
                "terms_ip": ip_address,
                "terms_user_agent": user_agent,
            }
        ).eq("id", user_id).execute()

    def record_phi_consent(
        self,
        user_id: int,
        *,
        phi_consent_version: str,
        ip_address: str,
        user_agent: str,
    ) -> None:
        # Cap at storage boundary; mirrors the SQLite backend + audit.record().
        self._t("users").update(
            {
                "phi_consent_at": _now_iso(),
                "phi_consent_version": phi_consent_version,
                "phi_consent_ip": ip_address[:IP_MAX_LENGTH] if ip_address else ip_address,
                "phi_consent_user_agent": user_agent[:USER_AGENT_MAX_LENGTH]
                if user_agent
                else user_agent,
            }
        ).eq("id", user_id).execute()

    def set_active_org(self, user_id: int, organization_id: int | None) -> None:
        self._t("users").update({"active_org_id": organization_id}).eq("id", user_id).execute()

    # --- Provider CRUD ---

    def save_provider(
        self, result: NPIResult, user_id: int, notes: str | None = None
    ) -> SavedProvider:
        provider = SavedProvider.from_npi_result(result, notes=notes)
        now = _now_iso()

        # Fetch existing to preserve appt_address, appt_suite, appt_phone, appt_fax, is_televisit, enrichment, and merge notes (matches SQLite behavior)
        existing = self.get_provider(provider.npi, user_id)
        appt_address = existing.appt_address if existing else None
        appt_suite = existing.appt_suite if existing else None
        appt_phone = existing.appt_phone if existing else None
        appt_fax = existing.appt_fax if existing else None
        is_televisit = existing.is_televisit if existing else False
        enrichment_json = existing.enrichment_json if existing else None
        merged_notes = (
            provider.notes if provider.notes is not None else (existing.notes if existing else None)
        )

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
                "notes": merged_notes,
                "appt_address": appt_address,
                "appt_suite": appt_suite,
                "appt_phone": appt_phone,
                "appt_fax": appt_fax,
                "is_televisit": is_televisit,
                "enrichment_json": enrichment_json,
                "saved_at": provider.saved_at.isoformat() if provider.saved_at else now,
                "updated_at": now,
            },
            on_conflict="user_id,npi",
        ).execute()
        logger.info("Saved provider %s: %s (user %s)", provider.npi, provider.display_name, user_id)
        return provider

    def get_provider(self, npi: str, user_id: int | None) -> SavedProvider | None:
        if user_id is None:
            return None
        result = (
            self._t("saved_providers").select("*").eq("npi", npi).eq("user_id", user_id).execute()
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

    def search_providers(self, user_id: int, query: str) -> list[SavedProvider]:
        # Fetch all providers and filter in Python to avoid PostgREST .or_()
        # escaping issues (commas, %, _ in query break the filter DSL string).
        all_providers = self.list_providers(user_id)
        query_lower = query.lower()
        matched = [
            p
            for p in all_providers
            if query_lower in (p.display_name or "").lower()
            or query_lower in (p.npi or "")
            or query_lower in (p.specialty or "").lower()
            or query_lower in (p.notes or "").lower()
            or query_lower in (p.address_city or "").lower()
        ]
        return sorted(matched, key=lambda p: fuzzy_score(p, query_lower), reverse=True)

    def delete_provider(self, npi: str, user_id: int) -> bool:
        result = self._t("saved_providers").delete().eq("npi", npi).eq("user_id", user_id).execute()
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

    def set_appt_suite(self, npi: str, suite: str | None, user_id: int) -> bool:
        # Requires manual Supabase migration before deploy:
        # ALTER TABLE docstats_saved_providers ADD COLUMN IF NOT EXISTS appt_suite TEXT;
        result = (
            self._t("saved_providers")
            .update({"appt_suite": suite.strip() if suite else None})
            .eq("npi", npi)
            .eq("user_id", user_id)
            .execute()
        )
        return len(result.data) > 0

    def clear_appt_address(self, npi: str, user_id: int) -> bool:
        result = (
            self._t("saved_providers")
            .update(
                {"appt_address": None, "appt_suite": None, "appt_phone": None, "appt_fax": None}
            )
            .eq("npi", npi)
            .eq("user_id", user_id)
            .execute()
        )
        return len(result.data) > 0

    def set_televisit(self, npi: str, is_televisit: bool, user_id: int) -> bool:
        # Requires manual Supabase migration before deploy:
        # ALTER TABLE docstats_saved_providers ADD COLUMN IF NOT EXISTS is_televisit BOOLEAN DEFAULT FALSE;
        result = (
            self._t("saved_providers")
            .update({"is_televisit": is_televisit})
            .eq("npi", npi)
            .eq("user_id", user_id)
            .execute()
        )
        return len(result.data) > 0

    def set_appt_contact(self, npi: str, phone: str | None, fax: str | None, user_id: int) -> bool:
        # Requires manual Supabase migration before deploy:
        # ALTER TABLE docstats_saved_providers ADD COLUMN IF NOT EXISTS appt_phone TEXT;
        # ALTER TABLE docstats_saved_providers ADD COLUMN IF NOT EXISTS appt_fax TEXT;
        result = (
            self._t("saved_providers")
            .update(
                {
                    "appt_phone": phone.strip() if phone else None,
                    "appt_fax": fax.strip() if fax else None,
                }
            )
            .eq("npi", npi)
            .eq("user_id", user_id)
            .execute()
        )
        return len(result.data) > 0

    def update_notes(self, npi: str, notes: str | None, user_id: int) -> bool:
        result = (
            self._t("saved_providers")
            .update({"notes": notes, "updated_at": _now_iso()})
            .eq("npi", npi)
            .eq("user_id", user_id)
            .execute()
        )
        return len(result.data) > 0

    def update_enrichment(self, npi: str, enrichment_json: str, user_id: int) -> bool:
        try:
            result = (
                self._t("saved_providers")
                .update({"enrichment_json": enrichment_json, "updated_at": _now_iso()})
                .eq("npi", npi)
                .eq("user_id", user_id)
                .execute()
            )
            return len(result.data) > 0
        except Exception:
            # Column may not exist yet — requires manual migration:
            # ALTER TABLE docstats_saved_providers ADD COLUMN enrichment_json TEXT;
            logger.warning("Failed to update enrichment_json — column may not exist in Postgres")
            return False

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
                searched_at=_parse_ts(r.get("searched_at")),
            )
            for r in result.data
        ]

    # --- ZIP code lookup ---

    def lookup_zip(self, zip_code: str) -> dict[str, str] | None:
        self._ensure_zip_table()
        result = (
            self._t("zip_codes").select("city,state").eq("zip_code", zip_code.strip()[:5]).execute()
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
        if not data_file.exists():
            logger.warning("ZIP code data file not found at %s", data_file)
            self._zip_loaded = True
            return
        data = json.loads(data_file.read_text())
        rows = [{"zip_code": z["zip"], "city": z["city"], "state": z["state"]} for z in data]
        try:
            for i in range(0, len(rows), 500):
                self._t("zip_codes").upsert(rows[i : i + 500], on_conflict="zip_code").execute()
            logger.info("Loaded %d ZIP codes into Supabase", len(data))
            self._zip_loaded = True
        except Exception:
            logger.exception("Failed to load ZIP codes into Supabase — will retry next request")

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
        row = {
            "actor_user_id": actor_user_id,
            "scope_user_id": scope_user_id,
            "scope_organization_id": scope_organization_id,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "metadata": metadata or {},
            "ip": ip,
            "user_agent": user_agent,
            "created_at": _now_iso(),
        }
        result = self._t("audit_events").insert(row).execute()
        return int(result.data[0]["id"])

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
        query = self._t("audit_events").select("*")
        if actor_user_id is not None:
            query = query.eq("actor_user_id", actor_user_id)
        if scope_user_id is not None:
            query = query.eq("scope_user_id", scope_user_id)
        if scope_organization_id is not None:
            query = query.eq("scope_organization_id", scope_organization_id)
        if entity_type is not None:
            query = query.eq("entity_type", entity_type)
        if entity_id is not None:
            query = query.eq("entity_id", entity_id)
        # Tiebreaker on id DESC so same-millisecond rows stay deterministic.
        result = query.order("created_at", desc=True).order("id", desc=True).limit(limit).execute()
        return [_row_to_audit_event(row) for row in result.data]

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
        row = {
            "name": name,
            "slug": slug,
            "npi": npi,
            "address_line1": address_line1,
            "address_line2": address_line2,
            "address_city": address_city,
            "address_state": address_state,
            "address_zip": address_zip,
            "phone": phone,
            "fax": fax,
            "terms_bundle_version": terms_bundle_version,
            "created_at": _now_iso(),
        }
        result = self._t("organizations").insert(row).execute()
        return _row_to_organization(result.data[0])

    def get_organization(self, organization_id: int) -> Organization | None:
        result = (
            self._t("organizations")
            .select("*")
            .eq("id", organization_id)
            .is_("deleted_at", None)
            .execute()
        )
        return _row_to_organization(result.data[0]) if result.data else None

    def get_organization_by_slug(self, slug: str) -> Organization | None:
        result = (
            self._t("organizations").select("*").eq("slug", slug).is_("deleted_at", None).execute()
        )
        return _row_to_organization(result.data[0]) if result.data else None

    def soft_delete_organization(self, organization_id: int) -> bool:
        result = (
            self._t("organizations")
            .update({"deleted_at": _now_iso()})
            .eq("id", organization_id)
            .is_("deleted_at", None)
            .execute()
        )
        return bool(result.data)

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
        # failing the UNIQUE constraint. Explicit deleted_at=None clears
        # the soft-delete marker.
        row = {
            "organization_id": organization_id,
            "user_id": user_id,
            "role": role,
            "invited_by_user_id": invited_by_user_id,
            "joined_at": _now_iso(),
            "deleted_at": None,
        }
        result = self._t("memberships").upsert(row, on_conflict="organization_id,user_id").execute()
        return _row_to_membership(result.data[0])

    def get_membership(self, organization_id: int, user_id: int) -> Membership | None:
        result = (
            self._t("memberships")
            .select("*")
            .eq("organization_id", organization_id)
            .eq("user_id", user_id)
            .is_("deleted_at", None)
            .execute()
        )
        return _row_to_membership(result.data[0]) if result.data else None

    def list_memberships_for_user(self, user_id: int) -> list[Membership]:
        result = (
            self._t("memberships")
            .select("*")
            .eq("user_id", user_id)
            .is_("deleted_at", None)
            .order("joined_at", desc=True)
            .order("id", desc=True)
            .execute()
        )
        return [_row_to_membership(row) for row in result.data]

    def list_memberships_for_org(self, organization_id: int) -> list[Membership]:
        result = (
            self._t("memberships")
            .select("*")
            .eq("organization_id", organization_id)
            .is_("deleted_at", None)
            .order("joined_at", desc=False)
            .order("id", desc=False)
            .execute()
        )
        return [_row_to_membership(row) for row in result.data]

    def update_membership_role(self, organization_id: int, user_id: int, role: str) -> bool:
        if role not in ROLES:
            raise ValueError(f"Unknown role: {role!r}")
        result = (
            self._t("memberships")
            .update({"role": role})
            .eq("organization_id", organization_id)
            .eq("user_id", user_id)
            .is_("deleted_at", None)
            .execute()
        )
        return bool(result.data)

    def soft_delete_membership(self, organization_id: int, user_id: int) -> bool:
        result = (
            self._t("memberships")
            .update({"deleted_at": _now_iso()})
            .eq("organization_id", organization_id)
            .eq("user_id", user_id)
            .is_("deleted_at", None)
            .execute()
        )
        return bool(result.data)

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
        row = {
            "id": session_id,
            "user_id": user_id,
            "data": data or {},
            "ip": ip,
            "user_agent": user_agent,
            "created_at": now.isoformat(),
            "last_seen_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        }
        result = self._t("sessions").insert(row).execute()
        return _row_to_session(result.data[0])

    def get_session(self, session_id: str) -> Session | None:
        result = self._t("sessions").select("*").eq("id", session_id).execute()
        return _row_to_session(result.data[0]) if result.data else None

    def touch_session(
        self,
        session_id: str,
        *,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> bool:
        updates: dict[str, Any] = {"last_seen_at": _now_iso()}
        if ip is not None:
            updates["ip"] = ip
        if user_agent is not None:
            updates["user_agent"] = user_agent
        result = (
            self._t("sessions")
            .update(updates)
            .eq("id", session_id)
            .is_("revoked_at", None)
            .execute()
        )
        return bool(result.data)

    def revoke_session(self, session_id: str) -> bool:
        result = (
            self._t("sessions")
            .update({"revoked_at": _now_iso()})
            .eq("id", session_id)
            .is_("revoked_at", None)
            .execute()
        )
        return bool(result.data)

    def list_sessions_for_user(self, user_id: int) -> list[Session]:
        result = (
            self._t("sessions")
            .select("*")
            .eq("user_id", user_id)
            .is_("revoked_at", None)
            .order("last_seen_at", desc=True)
            .order("id", desc=True)
            .execute()
        )
        return [_row_to_session(row) for row in result.data]

    def purge_expired_sessions(self) -> int:
        # supabase-py .delete() does not return the deleted rows — result.data
        # is always []. Select the ids first so the count is accurate.
        cutoff = _now_iso()
        to_delete = self._t("sessions").select("id").lt("expires_at", cutoff).execute()
        ids = [row["id"] for row in to_delete.data]
        if not ids:
            return 0
        self._t("sessions").delete().in_("id", ids).execute()
        return len(ids)

    # --- Patients (scope-enforced) ---

    def _apply_scope(self, query, scope: Scope):
        """Apply scope filtering to a supabase-py query chain.

        Mirrors ``scope.scope_sql_clause`` semantics — raises on anonymous,
        adds ``scope_user_id`` / ``scope_organization_id`` filters for the
        active mode (solo / org).
        """
        if scope.is_solo:
            return query.eq("scope_user_id", scope.user_id).is_("scope_organization_id", None)
        if scope.is_org:
            return query.eq("scope_organization_id", scope.organization_id).is_(
                "scope_user_id", None
            )
        raise ScopeRequired("Anonymous scope is not allowed for scoped entities")

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
        if scope.is_anonymous:
            raise ScopeRequired("Anonymous scope is not allowed for scoped entities")
        row: dict[str, Any] = {
            "scope_user_id": scope.user_id if scope.is_solo else None,
            "scope_organization_id": scope.organization_id if scope.is_org else None,
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
            "created_by_user_id": created_by_user_id,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        result = self._t("patients").insert(row).execute()
        return _row_to_patient(result.data[0])

    def get_patient(self, scope: Scope, patient_id: int) -> Patient | None:
        query = self._t("patients").select("*").eq("id", patient_id).is_("deleted_at", None)
        query = self._apply_scope(query, scope)
        result = query.execute()
        return _row_to_patient(result.data[0]) if result.data else None

    def list_patients(
        self,
        scope: Scope,
        *,
        search: str | None = None,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Patient]:
        query = self._t("patients").select("*")
        query = self._apply_scope(query, scope)
        if not include_deleted:
            query = query.is_("deleted_at", None)
        if search:
            # Fetch scope-filtered rows and filter in Python — matches the
            # search_providers approach in this backend and avoids PostgREST
            # escaping pitfalls with .or_() on user input.
            result = query.order("last_name").order("first_name").order("id").execute()
            term = search.strip().lower()
            rows = [
                r
                for r in result.data
                if (r.get("last_name", "") or "").lower().find(term) != -1
                or (r.get("first_name", "") or "").lower().find(term) != -1
                or (r.get("mrn", "") or "").lower().find(term) != -1
            ]
            return [_row_to_patient(r) for r in rows[offset : offset + limit]]
        result = (
            query.order("last_name")
            .order("first_name")
            .order("id")
            .range(offset, offset + limit - 1)
            .execute()
        )
        return [_row_to_patient(r) for r in result.data]

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
        fields: dict[str, Any] = {
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
        fields["updated_at"] = _now_iso()

        # Guard with scope + deleted_at so cross-tenant writes silently no-op
        # rather than corrupting another tenant's row.
        query = self._t("patients").update(fields).eq("id", patient_id).is_("deleted_at", None)
        query = self._apply_scope(query, scope)
        result = query.execute()
        if not result.data:
            return None
        return _row_to_patient(result.data[0])

    def soft_delete_patient(self, scope: Scope, patient_id: int) -> bool:
        query = (
            self._t("patients")
            .update({"deleted_at": _now_iso()})
            .eq("id", patient_id)
            .is_("deleted_at", None)
        )
        query = self._apply_scope(query, scope)
        result = query.execute()
        return bool(result.data)

    def close(self) -> None:
        pass  # supabase-py client has no close method
