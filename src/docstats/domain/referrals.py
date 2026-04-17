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


class InvalidTransition(ValueError):
    """Raised when a caller attempts a disallowed status transition."""


def transition_allowed(from_status: str, to_status: str) -> bool:
    """Return True if the ``(from, to)`` edge is in the state machine.

    Unknown ``from_status`` returns False (defensive — never crash on stale
    DB data). Use this in the ``update_status`` route to gate transitions.
    """
    allowed = STATUS_TRANSITIONS.get(from_status)
    if allowed is None:
        return False
    return to_status in allowed


def require_transition(from_status: str, to_status: str) -> None:
    """Raise :class:`InvalidTransition` unless the edge is allowed.

    Paired with :func:`transition_allowed` as an assertion variant — use this
    in storage helpers / route handlers where a failed transition must abort.
    """
    if not transition_allowed(from_status, to_status):
        raise InvalidTransition(
            f"Invalid referral status transition: {from_status!r} → {to_status!r}"
        )


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
