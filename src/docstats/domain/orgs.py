"""Organizations and memberships — dual-mode foundation.

A solo user has no organization. An org user may belong to 1..N organizations
and has exactly one "active" org at a time (``users.active_org_id``).

Roles form a simple hierarchy (higher index = more privilege). The hierarchy is
advisory; storage does NOT enforce transitions. UI / route handlers enforce
role-gated access via :func:`has_role_at_least`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from pydantic import BaseModel

# Role ladder (ascending privilege). Route handlers compare by index.
ROLES: Final[tuple[str, ...]] = (
    "read_only",
    "staff",
    "clinician",
    "coordinator",
    "admin",
    "owner",
)

DEFAULT_STALE_THRESHOLD_DAYS: Final[int] = 3
MIN_STALE_THRESHOLD_DAYS: Final[int] = 1
MAX_STALE_THRESHOLD_DAYS: Final[int] = 365

# Attachment retention (Phase 10.C).  7 years is the floor for most US
# healthcare record-retention regulations (HIPAA §164.316(b)(2)(i)
# mandates 6; state requirements often push it longer).  Legal teams
# typically tighten per-tenant; the admin UI surfaces this column so
# each org can set its own policy.
#
# ``MIN`` is deliberately high (30 days) — anything shorter means
# documents may purge before the initial delivery retries exhaust
# (Phase 9.E's 1h backoff cap, plus manual follow-up).  ``MAX`` caps
# at 30 years which is well beyond any healthcare retention rule.
DEFAULT_ATTACHMENT_RETENTION_DAYS: Final[int] = 2555  # ~7 years
MIN_ATTACHMENT_RETENTION_DAYS: Final[int] = 30
MAX_ATTACHMENT_RETENTION_DAYS: Final[int] = 10_950  # ~30 years


def has_role_at_least(role: str | None, required: str) -> bool:
    """Return True if ``role`` is at or above ``required`` in the ladder.

    Unknown roles (including None) return False. Required roles that aren't in
    :data:`ROLES` raise — callers should use a documented role name.
    """
    if role is None or role not in ROLES:
        return False
    if required not in ROLES:
        raise ValueError(f"Unknown required role: {required!r}")
    return ROLES.index(role) >= ROLES.index(required)


class Organization(BaseModel):
    """A clinic / physician-office tenant."""

    id: int
    name: str
    slug: str
    npi: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    address_city: str | None = None
    address_state: str | None = None
    address_zip: str | None = None
    phone: str | None = None
    fax: str | None = None
    terms_bundle_version: str | None = None
    stale_threshold_days: int = DEFAULT_STALE_THRESHOLD_DAYS
    attachment_retention_days: int = DEFAULT_ATTACHMENT_RETENTION_DAYS
    created_at: datetime
    deleted_at: datetime | None = None


class Membership(BaseModel):
    """A user's role in an organization."""

    id: int
    organization_id: int
    user_id: int
    role: str
    invited_by_user_id: int | None = None
    joined_at: datetime
    deleted_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.deleted_at is None
