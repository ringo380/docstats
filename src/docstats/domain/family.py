"""Family links — tracks relationships between adult user accounts.

Minor/dependent patient profiles live as Patient rows scoped to the parent
user; they do NOT have their own login.  Adult family members (spouse, adult
children, etc.) are separate User accounts linked bidirectionally via
FamilyLink rows.  One side sends an invitation; the other accepts it.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from docstats.domain.patients import Patient


RELATIONSHIP_VALUES: list[str] = [
    "spouse",
    "partner",
    "son",
    "daughter",
    "child",
    "parent",
    "guardian",
    "sibling",
    "other",
]


# Relationship labels that imply the dependent is a child of the initiator,
# i.e. eligible to be invited to manage their own account on turning 18.
CHILD_RELATIONSHIPS: frozenset[str] = frozenset({"son", "daughter", "child"})


class FamilyLink(BaseModel):
    """A link between two user accounts for family management."""

    id: int
    initiator_user_id: int
    linked_user_id: int
    relationship: str
    invite_token: str | None = None
    invite_email: str | None = None
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime
    # Set when this link was created via the "invite my dependent to manage
    # their own account" flow (#158); points at the Patient row that will be
    # re-parented on accept. None for ordinary adult linking.
    source_patient_id: int | None = None

    def is_pending(self) -> bool:
        return self.accepted_at is None and self.revoked_at is None

    def is_active(self) -> bool:
        return self.accepted_at is not None and self.revoked_at is None

    def is_dependent_upgrade(self) -> bool:
        return self.source_patient_id is not None


def patient_age(patient: Patient, today: date) -> int | None:
    """Whole years old today, or None if DOB missing/unparseable."""
    if not patient.date_of_birth:
        return None
    try:
        dob = date.fromisoformat(patient.date_of_birth)
    except ValueError:
        return None
    years = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    return years if years >= 0 else None


def is_eligible_for_self_upgrade(patient: Patient, today: date) -> bool:
    """True when a dependent Patient should be offered the self-upgrade invite.

    Eligibility = labelled as a child of the holder AND known DOB AND age ≥ 18.
    """
    if patient.relationship not in CHILD_RELATIONSHIPS:
        return False
    age = patient_age(patient, today)
    return age is not None and age >= 18


# Days before turning 18 to start surfacing the advance-notice hint on the
# parent's profile. Two months is enough to set expectations and (eventually)
# to schedule a transition appointment without becoming noisy.
UPCOMING_18_WINDOW_DAYS: int = 60


def upcoming_18_date(patient: Patient, today: date) -> date | None:
    """Return the date this child dependent turns 18, when it lies within
    the advance-notice window and the dependent is still a minor.

    Returns None when:
    - relationship isn't a child label
    - DOB is missing or unparseable
    - the 18th birthday is more than ``UPCOMING_18_WINDOW_DAYS`` away
    - the dependent is already 18+ (use ``is_eligible_for_self_upgrade``
      for that case so the invite button shows instead of the hint)
    """
    if patient.relationship not in CHILD_RELATIONSHIPS:
        return None
    if not patient.date_of_birth:
        return None
    try:
        dob = date.fromisoformat(patient.date_of_birth)
    except ValueError:
        return None
    try:
        eighteenth = dob.replace(year=dob.year + 18)
    except ValueError:
        # Feb 29 DOB in a non-leap target year — fall back to Feb 28.
        eighteenth = dob.replace(year=dob.year + 18, day=28)
    if eighteenth <= today:
        return None  # already eligible — invite UI takes over
    if (eighteenth - today).days > UPCOMING_18_WINDOW_DAYS:
        return None
    return eighteenth
