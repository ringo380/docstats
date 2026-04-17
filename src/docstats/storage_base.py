"""Abstract base class and shared helpers for storage backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from docstats.models import NPIResult, SavedProvider, SearchHistoryEntry

if TYPE_CHECKING:
    from docstats.domain.audit import AuditEvent
    from docstats.domain.orgs import Membership, Organization
    from docstats.domain.patients import Patient
    from docstats.domain.sessions import Session
    from docstats.scope import Scope


def normalize_email(email: str) -> str:
    """Normalize an email address for storage and lookup.

    Lowercases and strips whitespace. Does not validate format — callers that
    handle untrusted input should use ``docstats.validators.validate_email``
    first to reject malformed addresses with a friendly error.
    """
    return (email or "").strip().lower()


def fuzzy_score(provider: SavedProvider, query: str) -> float:
    """Score a saved provider against a search query for result ranking."""
    best = 0.0
    for field in (provider.display_name, provider.specialty, provider.notes):
        if field:
            ratio = SequenceMatcher(None, query, field.lower()).ratio()
            if ratio > best:
                best = ratio
    if provider.npi.startswith(query):
        best = max(best, 1.0)
    return best


class StorageBase(ABC):
    """Interface that both SQLite and Postgres storage backends implement."""

    # --- User CRUD ---

    @abstractmethod
    def create_user(self, email: str, password_hash: str) -> int: ...

    @abstractmethod
    def get_user_by_id(self, user_id: int) -> dict | None: ...

    @abstractmethod
    def get_user_by_email(self, email: str) -> dict | None: ...

    @abstractmethod
    def get_user_by_github_id(self, github_id: str) -> dict | None: ...

    @abstractmethod
    def upsert_github_user(
        self,
        github_id: str,
        github_login: str,
        email: str | None,
        display_name: str | None,
    ) -> int: ...

    @abstractmethod
    def update_last_login(self, user_id: int) -> None: ...

    @abstractmethod
    def set_user_pcp(self, user_id: int, pcp_npi: str) -> None: ...

    @abstractmethod
    def clear_user_pcp(self, user_id: int) -> None: ...

    @abstractmethod
    def update_user_profile(
        self,
        user_id: int,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        middle_name: str | None = None,
        date_of_birth: str | None = None,
        display_name: str | None = None,
    ) -> None: ...

    @abstractmethod
    def record_terms_acceptance(
        self,
        user_id: int,
        *,
        terms_version: str,
        ip_address: str,
        user_agent: str,
    ) -> None: ...

    @abstractmethod
    def record_phi_consent(
        self,
        user_id: int,
        *,
        phi_consent_version: str,
        ip_address: str,
        user_agent: str,
    ) -> None: ...

    @abstractmethod
    def set_active_org(self, user_id: int, organization_id: int | None) -> None: ...

    # --- Organizations & memberships ---

    @abstractmethod
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
    ) -> "Organization": ...

    @abstractmethod
    def get_organization(self, organization_id: int) -> "Organization | None": ...

    @abstractmethod
    def get_organization_by_slug(self, slug: str) -> "Organization | None": ...

    @abstractmethod
    def soft_delete_organization(self, organization_id: int) -> bool: ...

    @abstractmethod
    def create_membership(
        self,
        *,
        organization_id: int,
        user_id: int,
        role: str,
        invited_by_user_id: int | None = None,
    ) -> "Membership": ...

    @abstractmethod
    def get_membership(self, organization_id: int, user_id: int) -> "Membership | None": ...

    @abstractmethod
    def list_memberships_for_user(self, user_id: int) -> list["Membership"]: ...

    @abstractmethod
    def list_memberships_for_org(self, organization_id: int) -> list["Membership"]: ...

    @abstractmethod
    def update_membership_role(self, organization_id: int, user_id: int, role: str) -> bool: ...

    @abstractmethod
    def soft_delete_membership(self, organization_id: int, user_id: int) -> bool: ...

    # --- Provider CRUD ---

    @abstractmethod
    def save_provider(
        self, result: NPIResult, user_id: int, notes: str | None = None
    ) -> SavedProvider: ...

    @abstractmethod
    def get_provider(self, npi: str, user_id: int | None) -> SavedProvider | None: ...

    @abstractmethod
    def list_providers(self, user_id: int) -> list[SavedProvider]: ...

    @abstractmethod
    def search_providers(self, user_id: int, query: str) -> list[SavedProvider]: ...

    @abstractmethod
    def delete_provider(self, npi: str, user_id: int) -> bool: ...

    @abstractmethod
    def update_notes(self, npi: str, notes: str | None, user_id: int) -> bool: ...

    @abstractmethod
    def set_appt_address(self, npi: str, address: str, user_id: int) -> bool: ...

    @abstractmethod
    def set_appt_suite(self, npi: str, suite: str | None, user_id: int) -> bool: ...

    @abstractmethod
    def clear_appt_address(self, npi: str, user_id: int) -> bool: ...

    @abstractmethod
    def set_televisit(self, npi: str, is_televisit: bool, user_id: int) -> bool: ...

    @abstractmethod
    def set_appt_contact(
        self, npi: str, phone: str | None, fax: str | None, user_id: int
    ) -> bool: ...

    @abstractmethod
    def update_enrichment(self, npi: str, enrichment_json: str, user_id: int) -> bool: ...

    # --- Search history ---

    @abstractmethod
    def log_search(
        self, params: dict[str, str], result_count: int, user_id: int | None = None
    ) -> None: ...

    @abstractmethod
    def get_history(
        self, limit: int = 20, user_id: int | None = None
    ) -> list[SearchHistoryEntry]: ...

    # --- ZIP code lookup ---

    @abstractmethod
    def lookup_zip(self, zip_code: str) -> dict[str, str] | None: ...

    # --- Audit log (append-only) ---

    @abstractmethod
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
    ) -> int: ...

    @abstractmethod
    def list_audit_events(
        self,
        *,
        actor_user_id: int | None = None,
        scope_user_id: int | None = None,
        scope_organization_id: int | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int = 100,
    ) -> list["AuditEvent"]: ...

    # --- Sessions (server-side row for remote revocation) ---

    @abstractmethod
    def create_session(
        self,
        *,
        user_id: int | None = None,
        data: dict[str, Any] | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        ttl_seconds: int = 604800,
    ) -> "Session": ...

    @abstractmethod
    def get_session(self, session_id: str) -> "Session | None": ...

    @abstractmethod
    def touch_session(
        self,
        session_id: str,
        *,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> bool: ...

    @abstractmethod
    def revoke_session(self, session_id: str) -> bool: ...

    @abstractmethod
    def list_sessions_for_user(self, user_id: int) -> list["Session"]: ...

    @abstractmethod
    def purge_expired_sessions(self) -> int: ...

    # --- Patients (scope-enforced) ---

    @abstractmethod
    def create_patient(
        self,
        scope: "Scope",
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
    ) -> "Patient": ...

    @abstractmethod
    def get_patient(self, scope: "Scope", patient_id: int) -> "Patient | None": ...

    @abstractmethod
    def list_patients(
        self,
        scope: "Scope",
        *,
        search: str | None = None,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list["Patient"]: ...

    @abstractmethod
    def update_patient(
        self,
        scope: "Scope",
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
    ) -> "Patient | None": ...

    @abstractmethod
    def soft_delete_patient(self, scope: "Scope", patient_id: int) -> bool: ...

    @abstractmethod
    def close(self) -> None: ...
