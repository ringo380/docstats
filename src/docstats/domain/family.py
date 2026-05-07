"""Family links — tracks relationships between adult user accounts.

Minor/dependent patient profiles live as Patient rows scoped to the parent
user; they do NOT have their own login.  Adult family members (spouse, adult
children, etc.) are separate User accounts linked bidirectionally via
FamilyLink rows.  One side sends an invitation; the other accepts it.
"""

from __future__ import annotations

from datetime import datetime


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


class FamilyLink:
    """A link between two user accounts for family management."""

    def __init__(
        self,
        id: int,
        initiator_user_id: int,
        linked_user_id: int,
        relationship: str,
        invite_token: str | None,
        invite_email: str | None,
        accepted_at: datetime | None,
        revoked_at: datetime | None,
        created_at: datetime,
    ) -> None:
        self.id = id
        self.initiator_user_id = initiator_user_id
        self.linked_user_id = linked_user_id
        self.relationship = relationship
        self.invite_token = invite_token
        self.invite_email = invite_email
        self.accepted_at = accepted_at
        self.revoked_at = revoked_at
        self.created_at = created_at

    def is_pending(self) -> bool:
        return self.accepted_at is None and self.revoked_at is None

    def is_active(self) -> bool:
        return self.accepted_at is not None and self.revoked_at is None
