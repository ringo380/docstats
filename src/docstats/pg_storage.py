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


def _row_to_referral(row: dict) -> Referral:
    """Convert a Supabase referrals row into a Referral model."""
    created = _parse_ts(row.get("created_at"))
    updated = _parse_ts(row.get("updated_at"))
    assert created is not None and updated is not None
    return Referral(
        id=int(row["id"]),
        scope_user_id=row.get("scope_user_id"),
        scope_organization_id=row.get("scope_organization_id"),
        patient_id=int(row["patient_id"]),
        referring_provider_npi=row.get("referring_provider_npi"),
        referring_provider_name=row.get("referring_provider_name"),
        referring_organization=row.get("referring_organization"),
        receiving_provider_npi=row.get("receiving_provider_npi"),
        receiving_organization_name=row.get("receiving_organization_name"),
        specialty_code=row.get("specialty_code"),
        specialty_desc=row.get("specialty_desc"),
        reason=row.get("reason"),
        clinical_question=row.get("clinical_question"),
        urgency=row["urgency"],
        requested_service=row.get("requested_service"),
        diagnosis_primary_icd=row.get("diagnosis_primary_icd"),
        diagnosis_primary_text=row.get("diagnosis_primary_text"),
        payer_plan_id=row.get("payer_plan_id"),
        authorization_number=row.get("authorization_number"),
        authorization_status=row["authorization_status"],
        status=row["status"],
        assigned_to_user_id=row.get("assigned_to_user_id"),
        external_reference_id=row.get("external_reference_id"),
        external_source=row["external_source"],
        created_by_user_id=row.get("created_by_user_id"),
        created_at=created,
        updated_at=updated,
        deleted_at=_parse_ts(row.get("deleted_at")),
    )


def _row_to_referral_event(row: dict) -> ReferralEvent:
    """Convert a Supabase referral_events row into a ReferralEvent."""
    created = _parse_ts(row.get("created_at"))
    assert created is not None
    return ReferralEvent(
        id=int(row["id"]),
        referral_id=int(row["referral_id"]),
        event_type=row["event_type"],
        from_value=row.get("from_value"),
        to_value=row.get("to_value"),
        actor_user_id=row.get("actor_user_id"),
        note=row.get("note"),
        created_at=created,
    )


def _row_to_referral_diagnosis(row: dict) -> ReferralDiagnosis:
    created = _parse_ts(row.get("created_at"))
    assert created is not None
    return ReferralDiagnosis(
        id=int(row["id"]),
        referral_id=int(row["referral_id"]),
        icd10_code=row["icd10_code"],
        icd10_desc=row.get("icd10_desc"),
        is_primary=bool(row.get("is_primary", False)),
        source=row["source"],
        created_at=created,
    )


def _row_to_referral_medication(row: dict) -> ReferralMedication:
    created = _parse_ts(row.get("created_at"))
    assert created is not None
    return ReferralMedication(
        id=int(row["id"]),
        referral_id=int(row["referral_id"]),
        name=row["name"],
        dose=row.get("dose"),
        route=row.get("route"),
        frequency=row.get("frequency"),
        source=row["source"],
        created_at=created,
    )


def _row_to_referral_allergy(row: dict) -> ReferralAllergy:
    created = _parse_ts(row.get("created_at"))
    assert created is not None
    return ReferralAllergy(
        id=int(row["id"]),
        referral_id=int(row["referral_id"]),
        substance=row["substance"],
        reaction=row.get("reaction"),
        severity=row.get("severity"),
        source=row["source"],
        created_at=created,
    )


def _row_to_referral_attachment(row: dict) -> ReferralAttachment:
    created = _parse_ts(row.get("created_at"))
    assert created is not None
    return ReferralAttachment(
        id=int(row["id"]),
        referral_id=int(row["referral_id"]),
        kind=row["kind"],
        label=row["label"],
        date_of_service=row.get("date_of_service"),
        storage_ref=row.get("storage_ref"),
        checklist_only=bool(row.get("checklist_only", True)),
        source=row["source"],
        created_at=created,
    )


def _row_to_referral_response(row: dict) -> ReferralResponse:
    created = _parse_ts(row.get("created_at"))
    updated = _parse_ts(row.get("updated_at"))
    assert created is not None and updated is not None
    return ReferralResponse(
        id=int(row["id"]),
        referral_id=int(row["referral_id"]),
        appointment_date=row.get("appointment_date"),
        consult_completed=bool(row.get("consult_completed", False)),
        recommendations_text=row.get("recommendations_text"),
        attached_consult_note_ref=row.get("attached_consult_note_ref"),
        received_via=row["received_via"],
        recorded_by_user_id=row.get("recorded_by_user_id"),
        created_at=created,
        updated_at=updated,
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
        if scope.is_anonymous:
            raise ScopeRequired("Anonymous scope is not allowed for scoped entities")
        if urgency not in URGENCY_VALUES:
            raise ValueError(f"Unknown urgency: {urgency!r}")
        if authorization_status not in AUTH_STATUS_VALUES:
            raise ValueError(f"Unknown authorization_status: {authorization_status!r}")
        if status not in STATUS_VALUES:
            raise ValueError(f"Unknown status: {status!r}")
        if external_source not in EXTERNAL_SOURCE_VALUES:
            raise ValueError(f"Unknown external_source: {external_source!r}")
        if self.get_patient(scope, patient_id) is None:
            raise ValueError(f"Patient {patient_id} not found in scope or soft-deleted")

        row: dict[str, Any] = {
            "scope_user_id": scope.user_id if scope.is_solo else None,
            "scope_organization_id": scope.organization_id if scope.is_org else None,
            "patient_id": patient_id,
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
            "status": status,
            "assigned_to_user_id": assigned_to_user_id,
            "external_reference_id": external_reference_id,
            "external_source": external_source,
            "created_by_user_id": created_by_user_id,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        result = self._t("referrals").insert(row).execute()
        referral = _row_to_referral(result.data[0])
        # Seed a ``created`` event so the timeline is complete from t=0.
        self._t("referral_events").insert(
            {
                "referral_id": referral.id,
                "event_type": "created",
                "to_value": status,
                "actor_user_id": created_by_user_id,
                "created_at": _now_iso(),
            }
        ).execute()
        return referral

    def get_referral(self, scope: Scope, referral_id: int) -> Referral | None:
        query = self._t("referrals").select("*").eq("id", referral_id).is_("deleted_at", None)
        query = self._apply_scope(query, scope)
        result = query.execute()
        return _row_to_referral(result.data[0]) if result.data else None

    def list_referrals(
        self,
        scope: Scope,
        *,
        patient_id: int | None = None,
        status: str | None = None,
        assigned_to_user_id: int | None = None,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Referral]:
        query = self._t("referrals").select("*")
        query = self._apply_scope(query, scope)
        if not include_deleted:
            query = query.is_("deleted_at", None)
        if patient_id is not None:
            query = query.eq("patient_id", patient_id)
        if status is not None:
            query = query.eq("status", status)
        if assigned_to_user_id is not None:
            query = query.eq("assigned_to_user_id", assigned_to_user_id)
        result = (
            query.order("updated_at", desc=True)
            .order("id", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        return [_row_to_referral(r) for r in result.data]

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
        fields["updated_at"] = _now_iso()
        query = self._t("referrals").update(fields).eq("id", referral_id).is_("deleted_at", None)
        query = self._apply_scope(query, scope)
        result = query.execute()
        if not result.data:
            return None
        return _row_to_referral(result.data[0])

    def set_referral_status(
        self,
        scope: Scope,
        referral_id: int,
        new_status: str,
    ) -> Referral | None:
        if new_status not in STATUS_VALUES:
            raise ValueError(f"Unknown status: {new_status!r}")
        query = (
            self._t("referrals")
            .update({"status": new_status, "updated_at": _now_iso()})
            .eq("id", referral_id)
            .is_("deleted_at", None)
        )
        query = self._apply_scope(query, scope)
        result = query.execute()
        if not result.data:
            return None
        return _row_to_referral(result.data[0])

    def soft_delete_referral(self, scope: Scope, referral_id: int) -> bool:
        query = (
            self._t("referrals")
            .update({"deleted_at": _now_iso()})
            .eq("id", referral_id)
            .is_("deleted_at", None)
        )
        query = self._apply_scope(query, scope)
        result = query.execute()
        return bool(result.data)

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
        if self.get_referral(scope, referral_id) is None:
            return None
        row = {
            "referral_id": referral_id,
            "event_type": event_type,
            "from_value": from_value,
            "to_value": to_value,
            "actor_user_id": actor_user_id,
            "note": note,
            "created_at": _now_iso(),
        }
        result = self._t("referral_events").insert(row).execute()
        return _row_to_referral_event(result.data[0])

    def list_referral_events(
        self,
        scope: Scope,
        referral_id: int,
        *,
        limit: int = 100,
    ) -> list[ReferralEvent]:
        if self.get_referral(scope, referral_id) is None:
            return []
        result = (
            self._t("referral_events")
            .select("*")
            .eq("referral_id", referral_id)
            .order("created_at", desc=True)
            .order("id", desc=True)
            .limit(limit)
            .execute()
        )
        return [_row_to_referral_event(r) for r in result.data]

    # --- Referral clinical sub-entities (scope-transitive via referral) ---

    # --- Diagnoses ---

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
        result = (
            self._t("referral_diagnoses")
            .insert(
                {
                    "referral_id": referral_id,
                    "icd10_code": icd10_code,
                    "icd10_desc": icd10_desc,
                    "is_primary": is_primary,
                    "source": source,
                    "created_at": _now_iso(),
                }
            )
            .execute()
        )
        return _row_to_referral_diagnosis(result.data[0])

    def list_referral_diagnoses(self, scope: Scope, referral_id: int) -> list[ReferralDiagnosis]:
        if self.get_referral(scope, referral_id) is None:
            return []
        result = (
            self._t("referral_diagnoses")
            .select("*")
            .eq("referral_id", referral_id)
            .order("is_primary", desc=True)
            .order("id")
            .execute()
        )
        return [_row_to_referral_diagnosis(r) for r in result.data]

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
        fields: dict[str, Any] = {}
        if icd10_code is not None:
            fields["icd10_code"] = icd10_code
        if icd10_desc is not None:
            fields["icd10_desc"] = icd10_desc
        if is_primary is not None:
            fields["is_primary"] = is_primary
        if source is not None:
            fields["source"] = source
        if not fields:
            result = (
                self._t("referral_diagnoses")
                .select("*")
                .eq("id", diagnosis_id)
                .eq("referral_id", referral_id)
                .execute()
            )
            return _row_to_referral_diagnosis(result.data[0]) if result.data else None
        result = (
            self._t("referral_diagnoses")
            .update(fields)
            .eq("id", diagnosis_id)
            .eq("referral_id", referral_id)
            .execute()
        )
        return _row_to_referral_diagnosis(result.data[0]) if result.data else None

    def delete_referral_diagnosis(self, scope: Scope, referral_id: int, diagnosis_id: int) -> bool:
        if self.get_referral(scope, referral_id) is None:
            return False
        # supabase-py .delete() returns empty data — select first to get count.
        to_delete = (
            self._t("referral_diagnoses")
            .select("id")
            .eq("id", diagnosis_id)
            .eq("referral_id", referral_id)
            .execute()
        )
        if not to_delete.data:
            return False
        (
            self._t("referral_diagnoses")
            .delete()
            .eq("id", diagnosis_id)
            .eq("referral_id", referral_id)
            .execute()
        )
        return True

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
        result = (
            self._t("referral_medications")
            .insert(
                {
                    "referral_id": referral_id,
                    "name": name,
                    "dose": dose,
                    "route": route,
                    "frequency": frequency,
                    "source": source,
                    "created_at": _now_iso(),
                }
            )
            .execute()
        )
        return _row_to_referral_medication(result.data[0])

    def list_referral_medications(self, scope: Scope, referral_id: int) -> list[ReferralMedication]:
        if self.get_referral(scope, referral_id) is None:
            return []
        result = (
            self._t("referral_medications")
            .select("*")
            .eq("referral_id", referral_id)
            .order("id")
            .execute()
        )
        return [_row_to_referral_medication(r) for r in result.data]

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
            result = (
                self._t("referral_medications")
                .select("*")
                .eq("id", medication_id)
                .eq("referral_id", referral_id)
                .execute()
            )
            return _row_to_referral_medication(result.data[0]) if result.data else None
        result = (
            self._t("referral_medications")
            .update(fields)
            .eq("id", medication_id)
            .eq("referral_id", referral_id)
            .execute()
        )
        return _row_to_referral_medication(result.data[0]) if result.data else None

    def delete_referral_medication(
        self, scope: Scope, referral_id: int, medication_id: int
    ) -> bool:
        if self.get_referral(scope, referral_id) is None:
            return False
        to_delete = (
            self._t("referral_medications")
            .select("id")
            .eq("id", medication_id)
            .eq("referral_id", referral_id)
            .execute()
        )
        if not to_delete.data:
            return False
        (
            self._t("referral_medications")
            .delete()
            .eq("id", medication_id)
            .eq("referral_id", referral_id)
            .execute()
        )
        return True

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
        result = (
            self._t("referral_allergies")
            .insert(
                {
                    "referral_id": referral_id,
                    "substance": substance,
                    "reaction": reaction,
                    "severity": severity,
                    "source": source,
                    "created_at": _now_iso(),
                }
            )
            .execute()
        )
        return _row_to_referral_allergy(result.data[0])

    def list_referral_allergies(self, scope: Scope, referral_id: int) -> list[ReferralAllergy]:
        if self.get_referral(scope, referral_id) is None:
            return []
        result = (
            self._t("referral_allergies")
            .select("*")
            .eq("referral_id", referral_id)
            .order("id")
            .execute()
        )
        return [_row_to_referral_allergy(r) for r in result.data]

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
            result = (
                self._t("referral_allergies")
                .select("*")
                .eq("id", allergy_id)
                .eq("referral_id", referral_id)
                .execute()
            )
            return _row_to_referral_allergy(result.data[0]) if result.data else None
        result = (
            self._t("referral_allergies")
            .update(fields)
            .eq("id", allergy_id)
            .eq("referral_id", referral_id)
            .execute()
        )
        return _row_to_referral_allergy(result.data[0]) if result.data else None

    def delete_referral_allergy(self, scope: Scope, referral_id: int, allergy_id: int) -> bool:
        if self.get_referral(scope, referral_id) is None:
            return False
        to_delete = (
            self._t("referral_allergies")
            .select("id")
            .eq("id", allergy_id)
            .eq("referral_id", referral_id)
            .execute()
        )
        if not to_delete.data:
            return False
        (
            self._t("referral_allergies")
            .delete()
            .eq("id", allergy_id)
            .eq("referral_id", referral_id)
            .execute()
        )
        return True

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
        result = (
            self._t("referral_attachments")
            .insert(
                {
                    "referral_id": referral_id,
                    "kind": kind,
                    "label": label,
                    "date_of_service": date_of_service,
                    "storage_ref": storage_ref,
                    "checklist_only": checklist_only,
                    "source": source,
                    "created_at": _now_iso(),
                }
            )
            .execute()
        )
        return _row_to_referral_attachment(result.data[0])

    def list_referral_attachments(self, scope: Scope, referral_id: int) -> list[ReferralAttachment]:
        if self.get_referral(scope, referral_id) is None:
            return []
        result = (
            self._t("referral_attachments")
            .select("*")
            .eq("referral_id", referral_id)
            .order("id")
            .execute()
        )
        return [_row_to_referral_attachment(r) for r in result.data]

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
            fields["checklist_only"] = checklist_only
        if source is not None:
            fields["source"] = source
        if not fields:
            result = (
                self._t("referral_attachments")
                .select("*")
                .eq("id", attachment_id)
                .eq("referral_id", referral_id)
                .execute()
            )
            return _row_to_referral_attachment(result.data[0]) if result.data else None
        result = (
            self._t("referral_attachments")
            .update(fields)
            .eq("id", attachment_id)
            .eq("referral_id", referral_id)
            .execute()
        )
        return _row_to_referral_attachment(result.data[0]) if result.data else None

    def delete_referral_attachment(
        self, scope: Scope, referral_id: int, attachment_id: int
    ) -> bool:
        if self.get_referral(scope, referral_id) is None:
            return False
        to_delete = (
            self._t("referral_attachments")
            .select("id")
            .eq("id", attachment_id)
            .eq("referral_id", referral_id)
            .execute()
        )
        if not to_delete.data:
            return False
        (
            self._t("referral_attachments")
            .delete()
            .eq("id", attachment_id)
            .eq("referral_id", referral_id)
            .execute()
        )
        return True

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
        now = _now_iso()
        result = (
            self._t("referral_responses")
            .insert(
                {
                    "referral_id": referral_id,
                    "appointment_date": appointment_date,
                    "consult_completed": consult_completed,
                    "recommendations_text": recommendations_text,
                    "attached_consult_note_ref": attached_consult_note_ref,
                    "received_via": received_via,
                    "recorded_by_user_id": recorded_by_user_id,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            .execute()
        )
        return _row_to_referral_response(result.data[0])

    def list_referral_responses(self, scope: Scope, referral_id: int) -> list[ReferralResponse]:
        if self.get_referral(scope, referral_id) is None:
            return []
        result = (
            self._t("referral_responses")
            .select("*")
            .eq("referral_id", referral_id)
            .order("created_at", desc=True)
            .order("id", desc=True)
            .execute()
        )
        return [_row_to_referral_response(r) for r in result.data]

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
            fields["consult_completed"] = consult_completed
        if recommendations_text is not None:
            fields["recommendations_text"] = recommendations_text
        if attached_consult_note_ref is not None:
            fields["attached_consult_note_ref"] = attached_consult_note_ref
        if received_via is not None:
            fields["received_via"] = received_via
        if not fields:
            result = (
                self._t("referral_responses")
                .select("*")
                .eq("id", response_id)
                .eq("referral_id", referral_id)
                .execute()
            )
            return _row_to_referral_response(result.data[0]) if result.data else None
        fields["updated_at"] = _now_iso()
        result = (
            self._t("referral_responses")
            .update(fields)
            .eq("id", response_id)
            .eq("referral_id", referral_id)
            .execute()
        )
        return _row_to_referral_response(result.data[0]) if result.data else None

    def delete_referral_response(self, scope: Scope, referral_id: int, response_id: int) -> bool:
        if self.get_referral(scope, referral_id) is None:
            return False
        to_delete = (
            self._t("referral_responses")
            .select("id")
            .eq("id", response_id)
            .eq("referral_id", referral_id)
            .execute()
        )
        if not to_delete.data:
            return False
        (
            self._t("referral_responses")
            .delete()
            .eq("id", response_id)
            .eq("referral_id", referral_id)
            .execute()
        )
        return True

    def close(self) -> None:
        pass  # supabase-py client has no close method
