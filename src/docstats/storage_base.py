"""Abstract base class and shared helpers for storage backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from docstats.models import NPIResult, SavedProvider, SearchHistoryEntry

if TYPE_CHECKING:
    from datetime import datetime  # forward-ref for list_audit_events kwargs

    from docstats.domain.audit import AuditEvent
    from docstats.domain.invitations import Invitation
    from docstats.domain.orgs import Membership, Organization
    from docstats.domain.patients import Patient
    from docstats.domain.deliveries import Delivery, DeliveryAttempt, DeliveryQueueStats
    from docstats.domain.share_tokens import ShareToken
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
    from docstats.domain.eligibility import AvailityPayer, EligibilityCheck
    from docstats.domain.staff_access import StaffAccessGrant
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

    @abstractmethod
    def delete_user(self, user_id: int) -> list[str]:
        """Delete the user and all solo-scoped data.

        Returns storage_refs of any attachment blobs that must be deleted from
        the file backend by the caller (blob cleanup cannot happen inside the
        DB transaction).
        """
        ...

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
        stale_threshold_days: int = 3,
    ) -> "Organization": ...

    @abstractmethod
    def get_organization(self, organization_id: int) -> "Organization | None": ...

    @abstractmethod
    def get_organization_by_slug(self, slug: str) -> "Organization | None": ...

    @abstractmethod
    def soft_delete_organization(self, organization_id: int) -> bool: ...

    @abstractmethod
    def update_organization(
        self,
        organization_id: int,
        *,
        name: str | None = None,
        npi: str | None = None,
        address_line1: str | None = None,
        address_line2: str | None = None,
        address_city: str | None = None,
        address_state: str | None = None,
        address_zip: str | None = None,
        phone: str | None = None,
        fax: str | None = None,
        stale_threshold_days: int | None = None,
        attachment_retention_days: int | None = None,
        overwrite: bool = False,
    ) -> "Organization | None":
        """Update an org's mutable columns.

        ``slug`` is intentionally NOT editable via this method — changing it
        would break bookmarked URLs, stored references, and potentially any
        downstream integration that keyed on the slug. Use a dedicated
        admin-only migration if a slug must change.

        ``None`` kwargs mean "leave unchanged" by default, matching the
        contract on :meth:`update_specialty_rule` and
        :meth:`update_payer_rule`. Pass ``overwrite=True`` to write every
        kwarg literally — required by the admin save route so an empty
        form submission clears optional fields rather than silently
        preserving the prior value.

        Raises ``ValueError`` if ``overwrite=True`` and ``name`` is ``None``
        or empty — ``organizations.name`` is ``NOT NULL`` in the schema, so
        clearing it would violate the constraint on write. Callers (the
        admin route) validate ``name`` at the form boundary first.

        Returns the updated :class:`Organization`, or ``None`` if the row
        is missing or soft-deleted.
        """
        ...

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
    def list_all_organizations(self, *, include_deleted: bool = False) -> list["Organization"]:
        """Return every org row (live by default).  Platform-wide sweep
        helper — the retention job (10.C) iterates tenants via this.
        No scope gate: callers with cross-tenant authority are
        dispatcher-style background jobs, not user sessions."""

    @abstractmethod
    def update_membership_role(self, organization_id: int, user_id: int, role: str) -> bool: ...

    @abstractmethod
    def soft_delete_membership(self, organization_id: int, user_id: int) -> bool: ...

    # --- Organization invitations (Phase 6.F) ---

    @abstractmethod
    def create_invitation(
        self,
        *,
        organization_id: int,
        email: str,
        role: str,
        token: str,
        expires_at: "datetime",
        invited_by_user_id: int | None = None,
    ) -> "Invitation":
        """Insert a new invitation row and return it. The caller is
        responsible for generating the ``token`` (via
        :func:`docstats.domain.invitations.generate_token`) and
        ``expires_at`` (via
        :func:`docstats.domain.invitations.compute_expires_at`); both are
        passed in so the route layer can log an audit event referencing
        the token value it just created without a second read.

        Email is stored normalized (lowercased + stripped) — callers
        should pass the user's submitted value; the storage layer
        normalizes at the boundary. Role is NOT validated here;
        callers must validate via
        :func:`docstats.domain.invitations.validate_role` first.
        """
        ...

    @abstractmethod
    def get_invitation_by_token(self, token: str) -> "Invitation | None":
        """Look up an invitation by its secret token. Returns ``None`` if
        no row matches — the invitation may have been revoked (row still
        exists but ``revoked_at`` set), expired, already accepted, or
        never existed; callers use :meth:`Invitation.is_pending` to
        distinguish."""
        ...

    @abstractmethod
    def get_invitation(self, invitation_id: int) -> "Invitation | None":
        """Look up an invitation by its primary key."""
        ...

    @abstractmethod
    def list_invitations_for_org(
        self,
        organization_id: int,
        *,
        include_accepted: bool = False,
        include_revoked: bool = False,
        include_expired: bool = False,
    ) -> list["Invitation"]:
        """Return invitations for an org, newest first.

        By default returns only pending invitations — those that are
        neither accepted nor revoked nor expired. Pass the corresponding
        flag to include each non-pending class (useful for an "all
        invitations" admin view).
        """
        ...

    @abstractmethod
    def revoke_invitation(self, invitation_id: int) -> bool:
        """Set ``revoked_at`` to now. Returns True if the row was in a
        revokable state (pending) before the update, False otherwise
        (already accepted, already revoked, or missing). Idempotent: a
        second revoke on an already-revoked row returns False without
        changing the timestamp."""
        ...

    @abstractmethod
    def mark_invitation_accepted(self, invitation_id: int) -> bool:
        """Set ``accepted_at`` to now. Returns True if the row was in a
        pending state, False otherwise. Must be called in the same
        transaction/flow as :meth:`create_membership` so a token can't be
        re-used between the membership insert and the accept write."""
        ...

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
        action: str | None = None,
        since: "datetime | None" = None,
        until: "datetime | None" = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list["AuditEvent"]:
        """Return audit events matching all provided filters, newest first.

        All filters are AND'd; ``None`` means "no filter on this column".

        - ``action`` is an exact-match filter (use e.g. ``"patient.create"``).
        - ``since`` / ``until`` bound ``created_at`` (inclusive lower,
          EXCLUSIVE upper — mirrors the common "this hour" / "today" UI
          semantics and avoids boundary double-counting when chunking by
          day).
        - ``offset`` is for pagination (combined with ``limit``).

        Returns ``[]`` when no rows match.
        """
        ...

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

    # --- Staff access grants ---

    @abstractmethod
    def create_staff_access_grant(self, *, user_id: int, ttl_seconds: int) -> "StaffAccessGrant":
        """Create a new grant, revoking any existing active grant first."""
        ...

    @abstractmethod
    def get_active_staff_access_grant(self, user_id: int) -> "StaffAccessGrant | None":
        """Return the active (not revoked, not expired) grant for this user, or None."""
        ...

    @abstractmethod
    def revoke_staff_access_grant(self, user_id: int) -> int:
        """Revoke all active grants for a user. Returns count of rows updated."""
        ...

    @abstractmethod
    def list_staff_access_grants(
        self, user_id: int, *, limit: int = 20
    ) -> "list[StaffAccessGrant]":
        """Return grants for a user, newest first."""
        ...

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
    def get_patient(self, scope: "Scope | None", patient_id: int) -> "Patient | None": ...

    @abstractmethod
    def list_patients(
        self,
        scope: "Scope",
        *,
        search: str | None = None,
        mrn: str | None = None,
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
    def get_referral(self, scope: "Scope | None", referral_id: int) -> "Referral | None": ...

    @abstractmethod
    def list_referrals(
        self,
        scope: "Scope",
        *,
        patient_id: int | None = None,
        status: str | None = None,
        urgency: str | None = None,
        assigned_to_user_id: int | None = None,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list["Referral"]: ...

    @abstractmethod
    def count_referrals(
        self,
        scope: "Scope",
        *,
        assigned_to_user_id: int | None = None,
        statuses: tuple[str, ...] | None = None,
        updated_before: datetime | None = None,
        include_deleted: bool = False,
    ) -> int: ...

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
        method for the "I really do want to clear X" case. Only these four
        nullable fields may be cleared: ``assigned_to_user_id``,
        ``authorization_number``, ``payer_plan_id``, ``external_reference_id``.
        Other field names raise ``ValueError``.

        ``diagnosis_primary_icd`` and ``diagnosis_primary_text`` are
        intentionally NOT clearable via this method — they are denormalized
        from ``referral_diagnoses`` and must be changed by flipping or
        deleting the ``is_primary`` sub-table row (which triggers the sync
        helper). Clearing them directly would break the "sub-table is source
        of truth" invariant.
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
        scope: "Scope | None",
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
        scope: "Scope | None",
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
        scope: "Scope | None",
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
        scope: "Scope | None",
        referral_id: int,
    ) -> list["ReferralAttachment"]: ...

    @abstractmethod
    def get_referral_attachment(
        self,
        scope: "Scope",
        attachment_id: int,
    ) -> "ReferralAttachment | None":
        """Fetch one attachment by id, scope-gated via the parent referral.

        Returns None when the row doesn't exist OR the parent referral is
        outside the caller's scope.  Used by the attachment download /
        delete routes where the URL carries only the attachment id.
        """

    @abstractmethod
    def list_attachments_expired(
        self,
        cutoff_created_at: "datetime",
        *,
        scope_organization_id: int | None = None,
        scope_user_id: int | None = None,
        limit: int = 500,
    ) -> list["ReferralAttachment"]:
        """Phase 10.C — bucket-backed attachments older than ``cutoff`` in
        the specified scope.  Exactly one of ``scope_organization_id`` /
        ``scope_user_id`` must be provided; the retention sweep calls this
        once per tenant and processes results in batches of ``limit``.
        """

    @abstractmethod
    def list_solo_user_ids_with_attachments(self) -> list[int]:
        """Phase 10.C — distinct ``scope_user_id`` values that own at
        least one bucket-backed attachment.  Feeds the retention sweep
        so solo users get their attachments purged without enumerating
        every user row."""

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

    @abstractmethod
    def clear_referral_response_field(
        self,
        scope: "Scope",
        referral_id: int,
        response_id: int,
        field: str,
    ) -> "ReferralResponse | None":
        """Explicitly set a nullable response field back to ``NULL``.

        ``update_referral_response`` uses ``None``-means-skip semantics so
        partial updates don't wipe unrelated fields; this is the companion
        method for the "I really do want to clear X" case, matching the
        :meth:`clear_referral_field` pattern on the parent referral. Only
        the three nullable text columns may be cleared:
        ``appointment_date``, ``recommendations_text``,
        ``attached_consult_note_ref``. Other field names raise ``ValueError``.
        ``consult_completed`` and ``received_via`` are intentionally
        non-clearable: the former has a bool default and the latter has a
        NOT-NULL enum constraint with a ``manual`` default.
        """
        ...

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
        specialty_code: str | None = None,
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

        ``specialty_code`` narrows the result at the DB level (at most two rows
        — the global + any org override) so the rules engine doesn't scan the
        full rule set per resolve. Omit to return the full catalog.
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
        can restore a field to ``None`` that an admin previously filled in,
        and by the Phase 6.B admin route so clearing ``display_name`` via
        the edit form writes ``NULL`` through instead of being silently
        skipped. ``bump_version`` defaults to ``True``; callers that just
        fix seed typos or push canonical values back should pass ``False``
        so rule-engine caches aren't invalidated.

        With ``overwrite=True``, the concrete implementations raise
        ``ValueError`` if any known-NOT-NULL JSONB column
        (``required_fields``, ``recommended_attachments``,
        ``intake_questions``, ``urgency_red_flags``,
        ``common_rejection_reasons``) is ``None``. Callers must supply a
        dict (possibly empty, e.g. ``{}`` or ``{"fields": []}``) for each
        of those kwargs in overwrite mode.

        Returns the updated :class:`SpecialtyRule`, or ``None`` if the row
        is missing (TOCTOU — deleted between the caller's read and this
        write). Callers should treat ``None`` as "write didn't land" and
        NOT emit audit events against the vanished row.
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
        payer_key: str | None = None,
    ) -> list["PayerRule"]:
        """Return payer rules, ordering and merge semantics identical to
        :meth:`list_specialty_rules`. When ``include_globals=True`` with a
        concrete ``organization_id``, both the global default AND the org
        override for the same ``payer_key`` are returned as separate rows.
        The rules engine (Phase 3) owns the "org override wins" merge.

        ``payer_key`` narrows the result at the DB level (mirrors the
        ``specialty_code`` kwarg on ``list_specialty_rules``).
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
        ``None``, and lets the Phase 6.C admin route clear ``display_name``
        / ``notes`` / ``auth_typical_turnaround_days``). ``bump_version=False``
        skips the version_id bump for seed re-runs that restore canonical
        values.

        With ``overwrite=True``, the concrete implementations raise
        ``ValueError`` if ``auth_required_services`` or ``records_required``
        is ``None``. Both are NOT NULL JSONB columns in the schema —
        callers must supply a dict (``{}`` or ``{"services": []}`` are
        fine).

        Returns the updated :class:`PayerRule`, or ``None`` if the row is
        missing (TOCTOU). Callers should treat ``None`` as "write didn't
        land" and NOT emit audit events against the vanished row.
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

    # --- Deliveries (Phase 9.A, scope-transitive via referral) ---

    @abstractmethod
    def create_delivery(
        self,
        scope: "Scope",
        *,
        referral_id: int,
        channel: str,
        recipient: str,
        packet_artifact: dict[str, Any] | None = None,
        vendor_name: str | None = None,
        idempotency_key: str | None = None,
    ) -> "Delivery":
        """Create a new ``queued`` delivery row for a referral.

        Scope is validated at the boundary — the referral must be
        visible in ``scope`` or the call raises ``ValueError``. Scope
        columns on the delivery row are denormalized from the parent
        referral for fast admin list queries.
        """

    @abstractmethod
    def get_delivery(self, scope: "Scope | None", delivery_id: int) -> "Delivery | None":
        """Fetch a delivery.

        ``scope=None`` is the dispatcher-only path (no user session).
        User-facing callers MUST pass a non-None scope; the storage
        filters out rows the caller can't see.
        """

    @abstractmethod
    def get_delivery_by_vendor_message_id(self, vendor_message_id: str) -> "Delivery | None":
        """Look up a delivery by vendor-assigned message ID.

        Used by vendor webhook receivers to correlate callbacks to rows.
        No scope filter — webhook receivers are not user-session callers.
        """

    @abstractmethod
    def list_deliveries_for_referral(self, scope: "Scope", referral_id: int) -> list["Delivery"]:
        """List all deliveries for a referral, newest first."""

    @abstractmethod
    def cancel_delivery(
        self, scope: "Scope", delivery_id: int, *, cancelled_by_user_id: int
    ) -> bool:
        """Flip a non-terminal delivery to ``cancelled``. Returns True iff
        a row was actually transitioned. Terminal rows are idempotent no-ops
        (return False)."""

    @abstractmethod
    def list_deliveries_ready_for_dispatch(
        self, *, limit: int = 20, stuck_sending_seconds: int = 120
    ) -> list["Delivery"]:
        """Dispatcher-only: return ``queued`` rows + ``sending`` rows
        whose ``updated_at`` is older than ``stuck_sending_seconds``
        (those crashed/got-SIGTERM'd mid-send)."""

    @abstractmethod
    def mark_delivery_sending(self, delivery_id: int) -> None:
        """Flip to ``sending`` + bump ``updated_at``. Dispatcher calls
        this before invoking the channel."""

    @abstractmethod
    def mark_delivery_sent(
        self,
        delivery_id: int,
        *,
        vendor_name: str,
        vendor_message_id: str,
        status: str = "sent",
    ) -> None:
        """Record a successful vendor submission. ``status`` is
        ``"sent"`` (awaiting webhook-driven delivery confirmation) or
        ``"delivered"`` (vendor synchronously confirmed)."""

    @abstractmethod
    def mark_delivery_failed(
        self,
        delivery_id: int,
        *,
        error_code: str,
        error_message: str | None,
    ) -> None:
        """Flip to terminal ``failed`` state. Retry cap exhausted or
        fatal error."""

    @abstractmethod
    def requeue_delivery_for_retry(
        self,
        delivery_id: int,
        *,
        error_code: str,
        error_message: str | None,
    ) -> None:
        """Retryable failure — bump ``retry_count``, record last_error_*,
        keep status ``queued`` so the sweeper picks it up again."""

    @abstractmethod
    def record_delivery_attempt_start(self, *, delivery_id: int, attempt_number: int) -> int:
        """Create a new ``delivery_attempts`` row in ``in_progress`` state.
        Returns the attempt id so the completer can close it out."""

    @abstractmethod
    def record_delivery_attempt_complete(
        self,
        *,
        attempt_id: int,
        result: str,
        error_code: str | None = None,
        error_message: str | None = None,
        vendor_response_excerpt: str | None = None,
    ) -> None:
        """Close out an attempt row. ``result`` is one of
        ``success | retryable | fatal`` (closed vocab)."""

    @abstractmethod
    def list_delivery_attempts(self, scope: "Scope", delivery_id: int) -> list["DeliveryAttempt"]:
        """List all attempts for a delivery, oldest first. Scope-gated
        via the parent delivery → referral chain."""

    @abstractmethod
    def list_deliveries_for_admin(
        self,
        *,
        scope_organization_id: int | None = None,
        scope_user_id: int | None = None,
        channel: str | None = None,
        status: str | None = None,
        referral_id: int | None = None,
        since: "datetime | None" = None,
        until: "datetime | None" = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list["Delivery"]:
        """Admin-facing list of deliveries.  Returns rows newest-first.

        Must be called with ``scope_organization_id`` set for org admins
        (solo mode uses ``scope_user_id``).  ``since``/``until`` filter on
        ``created_at`` (inclusive lower, exclusive upper) mirroring
        ``list_audit_events``.  Empty filters mean "no filter on that column".
        """

    @abstractmethod
    def get_delivery_queue_stats(
        self,
        *,
        scope_organization_id: int | None = None,
        scope_user_id: int | None = None,
    ) -> "DeliveryQueueStats":
        """Return a snapshot of the live delivery queue — counts by status +
        oldest queued age.  Used by the health endpoint."""

    # --- Inbound webhook inbox (Phase 8.C, dead-lettered) ---

    # --- Share tokens (Phase 9.B) ---

    @abstractmethod
    def create_share_token(
        self,
        *,
        delivery_id: int,
        token_hash: str,
        expires_at: "datetime",
        second_factor_kind: str = "none",
        second_factor_hash: str | None = None,
    ) -> "ShareToken":
        """Persist a new share token.  ``token_hash`` must be the SHA-256
        hex digest of the plaintext; the plaintext is never stored here."""

    @abstractmethod
    def get_share_token_by_hash(self, token_hash: str) -> "ShareToken | None":
        """Look up a share token by its hash.  Returns ``None`` if not found."""

    @abstractmethod
    def increment_share_token_views(self, token_id: int) -> None:
        """Bump ``view_count`` and update ``last_viewed_at``."""

    @abstractmethod
    def increment_share_token_failures(self, token_id: int) -> None:
        """Bump ``failed_attempts``.  Callers revoke the token when the
        count reaches ``MAX_FAILED_ATTEMPTS``."""

    @abstractmethod
    def revoke_share_token(self, token_id: int) -> bool:
        """Set ``revoked_at``.  Idempotent — returns True iff state changed."""

    # --- Inbound webhook inbox (Phase 8.C, dead-lettered) ---

    @abstractmethod
    def record_inbound_webhook(
        self,
        *,
        source: str | None,
        payload_json: dict[str, Any],
        http_headers_json: dict[str, Any],
        signature: str | None,
        status: str = "received",
        notes: str | None = None,
    ) -> int:
        """Persist an HMAC-verified inbound webhook payload.

        No routing / handlers yet. Phase 9+ consumes these rows (delivery
        status callbacks, EHR pushes, etc.). ``status`` must be one of
        ``received | processed | discarded | invalid_signature`` — the
        DB enforces the CHECK constraint. ``http_headers_json`` should
        already be allowlist-filtered by the caller so raw proxy
        identifiers never hit the DB.
        """

    @abstractmethod
    def close(self) -> None: ...

    # --- Eligibility checks (Phase 11.A) ---

    @abstractmethod
    def create_eligibility_check(
        self,
        scope: "Scope",
        *,
        patient_id: int,
        availity_payer_id: str,
        payer_name: str | None = None,
        service_type: str,
        status: str,
        error_message: str | None = None,
        result_json: str | None = None,
        raw_response_json: str | None = None,
        checked_at: "datetime | None" = None,
    ) -> "EligibilityCheck":
        """Create an eligibility check record.  Returns the created row."""

    @abstractmethod
    def update_eligibility_check(
        self,
        check_id: int,
        *,
        status: str,
        error_message: str | None = None,
        result_json: str | None = None,
        raw_response_json: str | None = None,
        checked_at: "datetime | None" = None,
    ) -> None:
        """Update status / result of an existing eligibility check row."""

    @abstractmethod
    def get_latest_eligibility_check(
        self,
        scope: "Scope",
        patient_id: int,
        *,
        availity_payer_id: str | None = None,
        service_type: str | None = None,
    ) -> "EligibilityCheck | None":
        """Return the most recent eligibility check for a patient.

        If ``availity_payer_id`` and/or ``service_type`` are provided they
        narrow the search.
        """

    @abstractmethod
    def list_eligibility_checks(
        self,
        scope: "Scope",
        patient_id: int,
        *,
        limit: int = 20,
    ) -> "list[EligibilityCheck]":
        """Return eligibility checks for a patient, newest-first."""

    # --- Availity payer directory ---

    @abstractmethod
    def upsert_availity_payers(self, payers: "list[AvailityPayer]") -> int:
        """Bulk-upsert payer rows from a fresh API sync.  Returns count upserted."""

    @abstractmethod
    def list_availity_payers(
        self,
        *,
        search: str | None = None,
        limit: int = 500,
    ) -> "list[AvailityPayer]":
        """Return cached payer rows, optionally filtered by name substring."""

    @abstractmethod
    def count_availity_payers(self) -> int:
        """Return total number of cached payer rows."""

    @abstractmethod
    def get_availity_payer_last_synced(self) -> "datetime | None":
        """Return the most recent last_synced_at across all payer rows, or None."""

    @abstractmethod
    def link_insurance_plan_payer(
        self,
        plan_id: int,
        availity_payer_id: str | None,
    ) -> None:
        """Set or clear insurance_plans.availity_payer_id for one plan."""
