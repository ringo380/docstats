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
from typing import Any


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


class ScopeRequired(ValueError):
    """Raised when a storage method refuses an anonymous scope.

    Patient / referral / attachment rows carry an owning scope — there is no
    row that "belongs to nobody." Routes that hit these methods must go
    through a dep that rejects anonymous callers first; if one slips through,
    we fail loudly here rather than silently leaking across tenants.
    """


def scope_sql_clause(
    scope: Scope,
    *,
    user_col: str = "scope_user_id",
    org_col: str = "scope_organization_id",
) -> tuple[str, list[Any]]:
    """Return ``(sql_fragment, params)`` for filtering rows to the given scope.

    Solo mode → ``scope_user_id = ? AND scope_organization_id IS NULL``.
    Org mode  → ``scope_organization_id = ? AND scope_user_id IS NULL``.
    Anonymous → raises ``ScopeRequired``; patient-level reads/writes require a
    concrete owner. ``params`` is a list so callers can ``.extend()`` with
    additional heterogeneous filter values (search terms, limits, etc.).
    """
    if scope.is_solo:
        return (f"{user_col} = ? AND {org_col} IS NULL", [scope.user_id])
    if scope.is_org:
        return (f"{org_col} = ? AND {user_col} IS NULL", [scope.organization_id])
    raise ScopeRequired("Anonymous scope is not allowed for scoped entities")
