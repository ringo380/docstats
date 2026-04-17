"""Scope of access — solo vs. organization vs. anonymous.

Every Patient / Referral / Attachment row in the referral platform is scoped
either to a ``user_id`` (solo mode) or an ``organization_id`` (org mode), never
both. The :class:`Scope` dataclass is the ambient authorization context passed
to storage and domain calls. Routes construct it via ``routes._common.get_scope``.

Rule (enforced at the storage layer, not here): callers pass the matching scope
key to every read and write. Cross-tenant access is impossible by contract
because the scope key is on the SQL WHERE clause.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Scope:
    """Ambient authorization context."""

    user_id: int | None = None
    organization_id: int | None = None
    membership_role: str | None = None

    @property
    def is_anonymous(self) -> bool:
        return self.user_id is None and self.organization_id is None

    @property
    def is_solo(self) -> bool:
        return self.user_id is not None and self.organization_id is None

    @property
    def is_org(self) -> bool:
        return self.organization_id is not None

    def __post_init__(self) -> None:
        # membership_role is only meaningful in org mode.
        if self.membership_role is not None and self.organization_id is None:
            raise ValueError("membership_role requires organization_id")
