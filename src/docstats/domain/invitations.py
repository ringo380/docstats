"""Organization invitations — Phase 6.F.

Org admins generate a signed magic link for a specific email + role. The
invitee clicks the link, signs up or logs in (email must match), and
accepts — which creates a membership.

Phase 6.F ships the data model + redemption flow with copyable links.
Phase 9 wires email delivery so admins don't need to manually share the
link. Until then, the create-invitation response surfaces the URL for
copy-paste into an IM or email.

Token shape: URL-safe 32-byte ``secrets.token_urlsafe(32)`` — same as
session tokens (see ``domain/sessions.py``). Stored plaintext because:

- Single-use: once accepted, ``accepted_at`` is set and the row can't be
  reused.
- Short-lived: default TTL 7 days; admin can shorten.
- No sensitive reveal: possessing the token grants membership, not
  passwords or PHI. Failure mode is bounded.

If we ever need to raise the bar (e.g. HIPAA team tier), swap to a
SHA-256 hash of the token + lookup by hash.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Final

from pydantic import BaseModel

from docstats.domain.orgs import ROLES

# TTL defaults for generated tokens. Admins can override via the route
# layer but 7 days is a reasonable "stale invite" window — if the
# invitee hasn't acted in a week, they probably won't without a nudge
# from the admin who re-issues.
DEFAULT_INVITATION_TTL_SECONDS: Final[int] = 7 * 24 * 60 * 60

# Minimum and maximum TTL an admin can set. Prevents accidentally
# 0-second or 10-year invitations.
MIN_INVITATION_TTL_SECONDS: Final[int] = 60 * 60  # 1 hour
MAX_INVITATION_TTL_SECONDS: Final[int] = 90 * 24 * 60 * 60  # 90 days


class Invitation(BaseModel):
    """A pending or redeemed invitation to join an organization.

    ``token`` is the secret the invitee presents on the redemption URL.
    It's returned by :func:`create_invitation` once; callers that need
    the link must capture it then. Subsequent reads (list / get) return
    the same value — no hashing layer today, see module docstring.
    """

    id: int
    organization_id: int
    email: str
    role: str  # must be in ROLES
    token: str
    invited_by_user_id: int | None = None
    expires_at: datetime
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime

    def is_pending(self, *, now: datetime | None = None) -> bool:
        """Return True if the invitation is still redeemable — not
        revoked, not already accepted, not expired."""
        if self.accepted_at is not None or self.revoked_at is not None:
            return False
        if now is None:
            now = datetime.now(tz=timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp > now


def generate_token() -> str:
    """Return a URL-safe random token suitable for an invitation link."""
    return secrets.token_urlsafe(32)


def compute_expires_at(ttl_seconds: int) -> datetime:
    """Clamp the TTL to the allowed range and return an absolute UTC
    expiry timestamp. Callers that want the default should pass
    :data:`DEFAULT_INVITATION_TTL_SECONDS`.
    """
    bounded = max(MIN_INVITATION_TTL_SECONDS, min(MAX_INVITATION_TTL_SECONDS, int(ttl_seconds)))
    return datetime.now(tz=timezone.utc) + timedelta(seconds=bounded)


def validate_role(role: str) -> str:
    """Return the role if it's in :data:`docstats.domain.orgs.ROLES`;
    raise ``ValueError`` otherwise. Callers at the route boundary
    should always validate before passing to storage."""
    if role not in ROLES:
        raise ValueError(f"Unknown role: {role!r}. Must be one of {ROLES}.")
    return role
