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
    from docstats.domain.imports import CsvImport, CsvImportRow
    from docstats.domain.reference import InsurancePlan, PayerRule, SpecialtyRule
    from docstats.domain.referrals import (
        Referral,
        ReferralAllergy,
        ReferralAttachment,
        ReferralDiagnosis,
        ReferralEvent,
        ReferralMedication,
        ReferralResponse,
    )
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

    # --- Referrals (scope-enforced; patient_id scope-matched) ---

    @abstractmethod
    def create_referral(
        self,
        scope: "Scope",
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
    ) -> "Referral": ...

    @abstractmethod
    def get_referral(self, scope: "Scope", referral_id: int) -> "Referral | None": ...

    @abstractmethod
    def list_referrals(
        self,
        scope: "Scope",
        *,
        patient_id: int | None = None,
        status: str | None = None,
        assigned_to_user_id: int | None = None,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list["Referral"]: ...

    @abstractmethod
    def update_referral(
        self,
        scope: "Scope",
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
    ) -> "Referral | None": ...

    @abstractmethod
    def set_referral_status(
        self,
        scope: "Scope",
        referral_id: int,
        new_status: str,
    ) -> "Referral | None": ...

    @abstractmethod
    def soft_delete_referral(self, scope: "Scope", referral_id: int) -> bool: ...

    @abstractmethod
    def clear_referral_field(
        self,
        scope: "Scope",
        referral_id: int,
        field: str,
    ) -> "Referral | None":
        """Explicitly set a nullable referral field back to ``NULL``.

        ``update_referral`` uses ``None``-means-skip semantics on every kwarg
        so partial updates don't wipe unrelated fields; this is the companion
        method for the "I really do want to clear X" case. Only nullable
        fields may be cleared: ``assigned_to_user_id``, ``authorization_number``,
        ``payer_plan_id``, ``external_reference_id``, ``diagnosis_primary_icd``,
        ``diagnosis_primary_text``. Other fields raise ``ValueError``.
        """
        ...

    # --- Referral events (append-only; scope-transitive via referral) ---

    @abstractmethod
    def record_referral_event(
        self,
        scope: "Scope",
        referral_id: int,
        *,
        event_type: str,
        from_value: str | None = None,
        to_value: str | None = None,
        actor_user_id: int | None = None,
        note: str | None = None,
    ) -> "ReferralEvent | None": ...

    @abstractmethod
    def list_referral_events(
        self,
        scope: "Scope",
        referral_id: int,
        *,
        limit: int = 100,
    ) -> list["ReferralEvent"]: ...

    # --- Referral clinical sub-entities (scope-transitive via referral) ---

    @abstractmethod
    def add_referral_diagnosis(
        self,
        scope: "Scope",
        referral_id: int,
        *,
        icd10_code: str,
        icd10_desc: str | None = None,
        is_primary: bool = False,
        source: str = "user_entered",
    ) -> "ReferralDiagnosis | None": ...

    @abstractmethod
    def list_referral_diagnoses(
        self,
        scope: "Scope",
        referral_id: int,
    ) -> list["ReferralDiagnosis"]: ...

    @abstractmethod
    def update_referral_diagnosis(
        self,
        scope: "Scope",
        referral_id: int,
        diagnosis_id: int,
        *,
        icd10_code: str | None = None,
        icd10_desc: str | None = None,
        is_primary: bool | None = None,
        source: str | None = None,
    ) -> "ReferralDiagnosis | None": ...

    @abstractmethod
    def delete_referral_diagnosis(
        self,
        scope: "Scope",
        referral_id: int,
        diagnosis_id: int,
    ) -> bool: ...

    @abstractmethod
    def add_referral_medication(
        self,
        scope: "Scope",
        referral_id: int,
        *,
        name: str,
        dose: str | None = None,
        route: str | None = None,
        frequency: str | None = None,
        source: str = "user_entered",
    ) -> "ReferralMedication | None": ...

    @abstractmethod
    def list_referral_medications(
        self,
        scope: "Scope",
        referral_id: int,
    ) -> list["ReferralMedication"]: ...

    @abstractmethod
    def update_referral_medication(
        self,
        scope: "Scope",
        referral_id: int,
        medication_id: int,
        *,
        name: str | None = None,
        dose: str | None = None,
        route: str | None = None,
        frequency: str | None = None,
        source: str | None = None,
    ) -> "ReferralMedication | None": ...

    @abstractmethod
    def delete_referral_medication(
        self,
        scope: "Scope",
        referral_id: int,
        medication_id: int,
    ) -> bool: ...

    @abstractmethod
    def add_referral_allergy(
        self,
        scope: "Scope",
        referral_id: int,
        *,
        substance: str,
        reaction: str | None = None,
        severity: str | None = None,
        source: str = "user_entered",
    ) -> "ReferralAllergy | None": ...

    @abstractmethod
    def list_referral_allergies(
        self,
        scope: "Scope",
        referral_id: int,
    ) -> list["ReferralAllergy"]: ...

    @abstractmethod
    def update_referral_allergy(
        self,
        scope: "Scope",
        referral_id: int,
        allergy_id: int,
        *,
        substance: str | None = None,
        reaction: str | None = None,
        severity: str | None = None,
        source: str | None = None,
    ) -> "ReferralAllergy | None": ...

    @abstractmethod
    def delete_referral_allergy(
        self,
        scope: "Scope",
        referral_id: int,
        allergy_id: int,
    ) -> bool: ...

    @abstractmethod
    def add_referral_attachment(
        self,
        scope: "Scope",
        referral_id: int,
        *,
        kind: str,
        label: str,
        date_of_service: str | None = None,
        storage_ref: str | None = None,
        checklist_only: bool = True,
        source: str = "user_entered",
    ) -> "ReferralAttachment | None": ...

    @abstractmethod
    def list_referral_attachments(
        self,
        scope: "Scope",
        referral_id: int,
    ) -> list["ReferralAttachment"]: ...

    @abstractmethod
    def update_referral_attachment(
        self,
        scope: "Scope",
        referral_id: int,
        attachment_id: int,
        *,
        kind: str | None = None,
        label: str | None = None,
        date_of_service: str | None = None,
        storage_ref: str | None = None,
        checklist_only: bool | None = None,
        source: str | None = None,
    ) -> "ReferralAttachment | None": ...

    @abstractmethod
    def delete_referral_attachment(
        self,
        scope: "Scope",
        referral_id: int,
        attachment_id: int,
    ) -> bool: ...

    # --- Referral responses (closed-loop updates from receiving side) ---

    @abstractmethod
    def record_referral_response(
        self,
        scope: "Scope",
        referral_id: int,
        *,
        appointment_date: str | None = None,
        consult_completed: bool = False,
        recommendations_text: str | None = None,
        attached_consult_note_ref: str | None = None,
        received_via: str = "manual",
        recorded_by_user_id: int | None = None,
    ) -> "ReferralResponse | None": ...

    @abstractmethod
    def list_referral_responses(
        self,
        scope: "Scope",
        referral_id: int,
    ) -> list["ReferralResponse"]: ...

    @abstractmethod
    def update_referral_response(
        self,
        scope: "Scope",
        referral_id: int,
        response_id: int,
        *,
        appointment_date: str | None = None,
        consult_completed: bool | None = None,
        recommendations_text: str | None = None,
        attached_consult_note_ref: str | None = None,
        received_via: str | None = None,
    ) -> "ReferralResponse | None": ...

    @abstractmethod
    def delete_referral_response(
        self,
        scope: "Scope",
        referral_id: int,
        response_id: int,
    ) -> bool: ...

    # --- Insurance plans (scope-owned) ---

    @abstractmethod
    def create_insurance_plan(
        self,
        scope: "Scope",
        *,
        payer_name: str,
        plan_name: str | None = None,
        plan_type: str = "other",
        member_id_pattern: str | None = None,
        group_id_pattern: str | None = None,
        requires_referral: bool = False,
        requires_prior_auth: bool = False,
        notes: str | None = None,
    ) -> "InsurancePlan": ...

    @abstractmethod
    def get_insurance_plan(self, scope: "Scope", plan_id: int) -> "InsurancePlan | None": ...

    @abstractmethod
    def list_insurance_plans(
        self,
        scope: "Scope",
        *,
        include_deleted: bool = False,
    ) -> list["InsurancePlan"]: ...

    @abstractmethod
    def update_insurance_plan(
        self,
        scope: "Scope",
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
    ) -> "InsurancePlan | None": ...

    @abstractmethod
    def soft_delete_insurance_plan(self, scope: "Scope", plan_id: int) -> bool: ...

    # --- Specialty rules (platform default or org override) ---

    @abstractmethod
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
    ) -> "SpecialtyRule": ...

    @abstractmethod
    def get_specialty_rule(self, rule_id: int) -> "SpecialtyRule | None": ...

    @abstractmethod
    def list_specialty_rules(
        self,
        *,
        organization_id: int | None = None,
        include_globals: bool = True,
    ) -> list["SpecialtyRule"]:
        """Return specialty rules, sorted by ``(specialty_code, organization_id
        NULLS FIRST, id)`` when ``include_globals=True`` — callers can rely on
        the global row appearing immediately before any org override with the
        same ``specialty_code``.

        Note: when ``organization_id`` is set and ``include_globals=True``,
        both the platform default AND the org override for the same
        ``specialty_code`` are returned (two rows, distinguished by their
        ``organization_id`` column). The rules engine (Phase 3) is
        responsible for merging them — typically "org override wins". Callers
        that want only one row per ``specialty_code`` should pass
        ``include_globals=False``.
        """
        ...

    @abstractmethod
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
    ) -> "SpecialtyRule | None":
        """Update a specialty_rule row.

        ``None`` means "leave unchanged" by default. Pass ``overwrite=True`` to
        write every kwarg literally (including ``None``) — used by
        :func:`docstats.domain.seed.seed_platform_defaults` so seed re-runs
        can restore a field to ``None`` that an admin previously filled in.
        ``bump_version`` defaults to ``True``; callers that just fix seed
        typos or push canonical values back should pass ``False`` so
        rule-engine caches aren't invalidated.
        """
        ...

    @abstractmethod
    def delete_specialty_rule(self, rule_id: int) -> bool: ...

    # --- Payer rules (platform default or org override) ---

    @abstractmethod
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
    ) -> "PayerRule": ...

    @abstractmethod
    def get_payer_rule(self, rule_id: int) -> "PayerRule | None": ...

    @abstractmethod
    def list_payer_rules(
        self,
        *,
        organization_id: int | None = None,
        include_globals: bool = True,
    ) -> list["PayerRule"]:
        """Return payer rules, ordering and merge semantics identical to
        :meth:`list_specialty_rules`. When ``include_globals=True`` with a
        concrete ``organization_id``, both the global default AND the org
        override for the same ``payer_key`` are returned as separate rows.
        The rules engine (Phase 3) owns the "org override wins" merge.
        """
        ...

    @abstractmethod
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
    ) -> "PayerRule | None":
        """Same shape as :meth:`update_specialty_rule`: pass ``overwrite=True``
        to write all kwargs literally, including ``None`` (lets the seeder
        reset e.g. Medicare's ``auth_typical_turnaround_days`` back to
        ``None``). ``bump_version=False`` skips the version_id bump for seed
        re-runs that restore canonical values.
        """
        ...

    @abstractmethod
    def delete_payer_rule(self, rule_id: int) -> bool: ...

    # --- CSV imports (scope-owned) + import rows (scope-transitive) ---

    @abstractmethod
    def create_csv_import(
        self,
        scope: "Scope",
        *,
        original_filename: str,
        uploaded_by_user_id: int | None = None,
        row_count: int = 0,
        mapping: dict[str, Any] | None = None,
    ) -> "CsvImport": ...

    @abstractmethod
    def get_csv_import(self, scope: "Scope", import_id: int) -> "CsvImport | None": ...

    @abstractmethod
    def list_csv_imports(
        self,
        scope: "Scope",
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list["CsvImport"]: ...

    @abstractmethod
    def update_csv_import(
        self,
        scope: "Scope",
        import_id: int,
        *,
        status: str | None = None,
        row_count: int | None = None,
        mapping: dict[str, Any] | None = None,
        error_report: dict[str, Any] | None = None,
    ) -> "CsvImport | None": ...

    @abstractmethod
    def delete_csv_import(self, scope: "Scope", import_id: int) -> bool: ...

    @abstractmethod
    def add_csv_import_row(
        self,
        scope: "Scope",
        import_id: int,
        *,
        row_index: int,
        raw_json: dict[str, Any] | None = None,
        validation_errors: dict[str, Any] | None = None,
        status: str = "pending",
    ) -> "CsvImportRow | None": ...

    @abstractmethod
    def list_csv_import_rows(
        self,
        scope: "Scope",
        import_id: int,
        *,
        status: str | None = None,
        limit: int = 2000,
        offset: int = 0,
    ) -> list["CsvImportRow"]: ...

    @abstractmethod
    def update_csv_import_row(
        self,
        scope: "Scope",
        import_id: int,
        row_id: int,
        *,
        raw_json: dict[str, Any] | None = None,
        validation_errors: dict[str, Any] | None = None,
        status: str | None = None,
        referral_id: int | None = None,
    ) -> "CsvImportRow | None": ...

    @abstractmethod
    def delete_csv_import_row(self, scope: "Scope", import_id: int, row_id: int) -> bool: ...

    @abstractmethod
    def close(self) -> None: ...
