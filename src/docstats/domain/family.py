"""Family links — tracks relationships between adult user accounts.

Minor/dependent patient profiles live as Patient rows scoped to the parent
user; they do NOT have their own login.  Adult family members (spouse, adult
children, etc.) are separate User accounts linked bidirectionally via
FamilyLink rows.  One side sends an invitation; the other accepts it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


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

    def is_pending(self) -> bool:
        return self.accepted_at is None and self.revoked_at is None

    def is_active(self) -> bool:
        return self.accepted_at is not None and self.revoked_at is None
