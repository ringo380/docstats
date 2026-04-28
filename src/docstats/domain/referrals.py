"""Referral lifecycle — the central object of the platform.

A Referral is scope-owned (exactly one of ``scope_user_id`` /
``scope_organization_id``) and always references a Patient in the same scope.
Scope-match of the patient is enforced at the storage layer: ``create_referral``
refuses if the ``patient_id`` isn't readable in the same scope.

State machine:

    draft ─────────┐
      │            │
      │            ├─→ cancelled ────(terminal)
      ▼            │
    ready ────────┤
      │            │
      ▼            │
    sent ─────────┤
      │            │
      ├─→ awaiting_records ──┐
      ├─→ awaiting_auth     ─┤
      │                       │
      ▼                       ▼
    scheduled ←──────────────┘
      │
      ├─→ completed (terminal)
      └─→ rejected ─→ draft (re-work loop) / cancelled

``STATUS_TRANSITIONS`` is a whitelist of allowed (from, to) edges;
:func:`transition_allowed` is the one place any caller should validate a
transition. The domain module is FastAPI-free so tests can exercise it in
isolation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from pydantic import BaseModel

from docstats.domain.orgs import ROLES

# --- Enumerations ---

URGENCY_VALUES: Final[tuple[str, ...]] = ("routine", "priority", "urgent", "stat")

STATUS_VALUES: Final[tuple[str, ...]] = (
    "draft",
    "ready",
    "sent",
    "awaiting_records",
    "awaiting_auth",
    "scheduled",
    "rejected",
    "completed",
    "cancelled",
)

AUTH_STATUS_VALUES: Final[tuple[str, ...]] = (
    "not_required",
    "required_pending",
    "obtained",
    "denied",
    "na_unknown",
)

EXTERNAL_SOURCE_VALUES: Final[tuple[str, ...]] = ("manual", "bulk_csv", "api")

EVENT_TYPE_VALUES: Final[tuple[str, ...]] = (
    "created",
    "status_changed",
    "field_edited",
    "exported",
    "sent",
    "response_received",
    "note_added",
    "assigned",
    "unassigned",
    # Phase 9 delivery lifecycle:
    "dispatched",  # delivery row moved from queued to sending
    "delivered",  # vendor confirmed end-recipient receipt
    "delivery_failed",  # retries exhausted or fatal error
)

# Provenance tag on every clinical sub-entity row (diagnoses, meds, allergies,
# attachments). The provenance model is a non-negotiable product principle —
# every piece of clinical data must know where it came from. "ai_draft" rows
# stay visually distinct in the UI until a user confirms/edits them.
SOURCE_VALUES: Final[tuple[str, ...]] = (
    "user_entered",
    "imported_csv",
    "nppes",
    "ai_draft",
    "carry_forward",
    "ehr_import",
)

# Attachment kinds — referral_attachments only. A "checklist_only" row means
# "the receiving specialist should expect this record, but we're not shipping
# the file through this platform yet" (file upload lands in Phase 10).
ATTACHMENT_KIND_VALUES: Final[tuple[str, ...]] = (
    "lab",
    "imaging",
    "note",
    "procedure",
    "medication_list",
    "problem_list",
    "other",
)

# Channel the closed-loop response arrived through. ``manual`` covers any
# out-of-band entry (the coordinator heard back by phone and typed it in);
# ``api`` is reserved for the future webhook receiver (Phase 9).
RECEIVED_VIA_VALUES: Final[tuple[str, ...]] = (
    "fax",
    "portal",
    "email",
    "phone",
    "manual",
    "api",
)

# --- State machine ---

# Directed adjacency list. Terminal states (completed, cancelled) have no
# outgoing edges — once there, a referral stays there.
STATUS_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "draft": frozenset({"ready", "cancelled"}),
    "ready": frozenset({"sent", "draft", "cancelled"}),
    "sent": frozenset({"awaiting_records", "awaiting_auth", "scheduled", "rejected", "cancelled"}),
    "awaiting_records": frozenset({"sent", "scheduled", "rejected", "cancelled"}),
    "awaiting_auth": frozenset({"sent", "scheduled", "rejected", "cancelled"}),
    "scheduled": frozenset({"completed", "rejected", "cancelled"}),
    "rejected": frozenset({"draft", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}

TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"completed", "cancelled"})
STATUS_TRANSITION_ROLES: Final[frozenset[str]] = frozenset(
    role for role in ROLES if role != "read_only"
)


class InvalidTransition(ValueError):
    """Raised when a caller attempts a disallowed status transition."""


class TransitionRoleDenied(PermissionError):
    """Raised when an org membership role cannot change referral status."""


def transition_allowed(from_status: str, to_status: str) -> bool:
    """Return True if the ``(from, to)`` edge is in the state machine.

    Unknown ``from_status`` returns False (defensive — never crash on stale
    DB data). Use this in the ``update_status`` route to gate transitions.
    """
    allowed = STATUS_TRANSITIONS.get(from_status)
    if allowed is None:
        return False
    return to_status in allowed


def role_can_transition_status(membership_role: str | None, *, is_org: bool) -> bool:
    """Return whether this scope can apply referral status transitions.

    Solo scopes have no membership role and retain the existing transition
    behavior. Org scopes fail closed: only documented non-read-only roles can
    change status, and unknown / missing roles cannot.
    """
    if not is_org:
        return True
    return membership_role in STATUS_TRANSITION_ROLES


def transition_allowed_for_role(
    from_status: str,
    to_status: str,
    membership_role: str | None,
    *,
    is_org: bool,
) -> bool:
    """Return True if both role policy and state-machine edge allow a move."""
    return role_can_transition_status(membership_role, is_org=is_org) and transition_allowed(
        from_status, to_status
    )


def require_transition(from_status: str, to_status: str) -> None:
    """Raise :class:`InvalidTransition` unless the edge is allowed.

    Paired with :func:`transition_allowed` as an assertion variant — use this
    in storage helpers / route handlers where a failed transition must abort.
    """
    if not transition_allowed(from_status, to_status):
        raise InvalidTransition(
            f"Invalid referral status transition: {from_status!r} → {to_status!r}"
        )


def require_transition_for_role(
    from_status: str,
    to_status: str,
    membership_role: str | None,
    *,
    is_org: bool,
) -> None:
    """Raise unless both the membership role and transition edge allow a move."""
    if not role_can_transition_status(membership_role, is_org=is_org):
        raise TransitionRoleDenied("This membership role cannot change referral status.")
    require_transition(from_status, to_status)


# --- Pydantic models ---


class Referral(BaseModel):
    """A referral request — the platform's central object."""

    id: int
    scope_user_id: int | None = None
    scope_organization_id: int | None = None
    patient_id: int

    # Referring side — who's sending the referral.
    referring_provider_npi: str | None = None
    referring_provider_name: str | None = None
    referring_organization: str | None = None

    # Receiving side — who the patient is being referred to.
    receiving_provider_npi: str | None = None
    receiving_organization_name: str | None = None

    # Specialty targeting (NUCC taxonomy).
    specialty_code: str | None = None
    specialty_desc: str | None = None

    # Clinical context.
    reason: str | None = None
    clinical_question: str | None = None
    urgency: str = "routine"  # must be in URGENCY_VALUES
    requested_service: str | None = None

    # Primary diagnosis headline; full list lives in referral_diagnoses (1.C).
    diagnosis_primary_icd: str | None = None
    diagnosis_primary_text: str | None = None

    # Payer + auth. payer_plan_id FKs into insurance_plans (1.E) — nullable.
    payer_plan_id: int | None = None
    authorization_number: str | None = None
    authorization_status: str = "na_unknown"  # must be in AUTH_STATUS_VALUES

    # Lifecycle.
    status: str = "draft"  # must be in STATUS_VALUES
    assigned_to_user_id: int | None = None

    # External linkage for bulk import / API sources.
    external_reference_id: str | None = None
    external_source: str = "manual"  # must be in EXTERNAL_SOURCE_VALUES

    # EHR write-back: Epic FHIR ServiceRequest.id written on referral creation.
    ehr_service_request_id: str | None = None

    created_by_user_id: int | None = None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class ReferralEvent(BaseModel):
    """An append-only lifecycle log entry for a Referral.

    Events scope themselves via their parent Referral — there are no
    scope_user_id / scope_organization_id columns on the event row. Access
    control flows through the Referral (callers read events by
    ``list_referral_events(scope, referral_id)`` which first validates scope).
    """

    id: int
    referral_id: int
    event_type: str  # must be in EVENT_TYPE_VALUES
    from_value: str | None = None
    to_value: str | None = None
    actor_user_id: int | None = None
    note: str | None = None
    created_at: datetime


# --- Clinical sub-entities (Phase 1.C) ---
#
# Each of the four sub-entity models below hangs off a Referral via
# ``referral_id``. Scope flows transitively through the parent referral —
# storage methods verify scope by calling ``get_referral(scope, referral_id)``
# before touching the sub-entity row.
#
# Hard-delete on the sub-entity is fine: the referral_events timeline captures
# the edit (``field_edited`` / ``note_added``), so a removed diagnosis still
# leaves an audit trail without keeping a tombstone row around.


class ReferralDiagnosis(BaseModel):
    """An ICD-10 diagnosis attached to a Referral.

    Exactly one row per referral may have ``is_primary = True`` (enforced by a
    partial unique index on ``(referral_id) WHERE is_primary``). The headline
    diagnosis is also denormalized onto ``referrals.diagnosis_primary_icd`` /
    ``referrals.diagnosis_primary_text`` for fast workspace-queue rendering.
    Storage owns that sync: ``add_referral_diagnosis``,
    ``update_referral_diagnosis``, and ``delete_referral_diagnosis`` all call
    an internal ``_sync_referral_primary_diagnosis`` helper whenever the
    primary bit is touched, so the headline never drifts from the sub-table.
    """

    id: int
    referral_id: int
    icd10_code: str
    icd10_desc: str | None = None
    is_primary: bool = False
    source: str = "user_entered"  # must be in SOURCE_VALUES
    created_at: datetime


class ReferralMedication(BaseModel):
    """A current medication on a Referral. Free-text; future phases may add
    RxNorm coding."""

    id: int
    referral_id: int
    name: str
    dose: str | None = None
    route: str | None = None
    frequency: str | None = None
    source: str = "user_entered"
    created_at: datetime


class ReferralAllergy(BaseModel):
    """An allergy on a Referral. Severity is free-text (mild / moderate /
    severe / anaphylactic) — not enum-constrained yet."""

    id: int
    referral_id: int
    substance: str
    reaction: str | None = None
    severity: str | None = None
    source: str = "user_entered"
    created_at: datetime


class ReferralAttachment(BaseModel):
    """A supporting record attached to a Referral.

    ``checklist_only = True`` means the record will be sent outside the
    platform (fax, portal upload) — we track that it should be included
    without storing the file. File upload to Supabase Storage lands in
    Phase 10; until then ``storage_ref`` stays None.
    """

    id: int
    referral_id: int
    kind: str  # must be in ATTACHMENT_KIND_VALUES
    label: str
    date_of_service: str | None = None  # ISO YYYY-MM-DD
    storage_ref: str | None = None  # reserved for Phase 10
    checklist_only: bool = True
    source: str = "user_entered"
    created_at: datetime


class ReferralResponse(BaseModel):
    """A closed-loop update on a Referral from the receiving side.

    Multiple responses per referral are allowed — e.g. one when the
    specialist's office schedules (appointment_date set, consult_completed
    False), a later one when the consult actually happens and
    recommendations land (consult_completed True, recommendations_text
    populated). Marking the terminal response triggers
    ``referrals.status → completed`` via the route layer (Phase 2+).

    ``attached_consult_note_ref`` is a Phase 10 storage key placeholder —
    until file upload ships, the recommendations live in the text column.
    """

    id: int
    referral_id: int
    appointment_date: str | None = None  # ISO YYYY-MM-DD
    consult_completed: bool = False
    recommendations_text: str | None = None
    attached_consult_note_ref: str | None = None  # reserved for Phase 10
    received_via: str = "manual"  # must be in RECEIVED_VIA_VALUES
    recorded_by_user_id: int | None = None
    created_at: datetime
    updated_at: datetime


# --- Completeness (Phase 2.D baseline; replaced by Phase 3 rules engine) ---


class CompletenessItem(BaseModel):
    """A single check result for the completeness panel."""

    code: str  # stable identifier — used as the dedupe key when Phase 3 merges
    label: str  # human-readable description, shown in the UI
    required: bool  # True = blocks "ready"; False = recommended-only
    satisfied: bool


class CompletenessReport(BaseModel):
    """Result of evaluating a referral against a rule set."""

    items: list[CompletenessItem]

    @property
    def missing_required(self) -> list[CompletenessItem]:
        return [i for i in self.items if i.required and not i.satisfied]

    @property
    def missing_recommended(self) -> list[CompletenessItem]:
        return [i for i in self.items if not i.required and not i.satisfied]

    @property
    def is_complete(self) -> bool:
        return not self.missing_required


def baseline_completeness(referral: Referral) -> CompletenessReport:
    """Minimum-viable completeness check — not specialty-aware.

    The Phase 3 rules engine merges specialty + payer rules on top of this
    baseline. Until then every referral goes through the same check.
    """

    def _nonblank(v: str | None) -> bool:
        return bool(v and v.strip())

    items = [
        CompletenessItem(
            code="reason",
            label="Reason for referral",
            required=True,
            satisfied=_nonblank(referral.reason),
        ),
        CompletenessItem(
            code="receiving_side",
            label="Receiving provider (NPI) or organization name",
            required=True,
            satisfied=_nonblank(referral.receiving_provider_npi)
            or _nonblank(referral.receiving_organization_name),
        ),
        CompletenessItem(
            code="specialty",
            label="Specialty description",
            required=True,
            satisfied=_nonblank(referral.specialty_desc),
        ),
        CompletenessItem(
            code="clinical_question",
            label="Specific clinical question",
            required=False,
            satisfied=_nonblank(referral.clinical_question),
        ),
        CompletenessItem(
            code="primary_diagnosis",
            label="Primary diagnosis (ICD-10)",
            required=False,
            satisfied=_nonblank(referral.diagnosis_primary_icd)
            or _nonblank(referral.diagnosis_primary_text),
        ),
        CompletenessItem(
            code="referring_side",
            label="Referring provider name",
            required=False,
            satisfied=_nonblank(referral.referring_provider_name),
        ),
    ]
    return CompletenessReport(items=items)
