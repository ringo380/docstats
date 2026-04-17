"""Server-side session rows.

The signed session cookie (Starlette ``SessionMiddleware``) still carries the
cookie blob, but since Phase 0.C it also carries a ``session_id`` pointing at a
row in ``sessions``. The DB row is the authoritative record of session state:

- Revoking a row invalidates the cookie on the next request (remote logout).
- Rows carry ``ip`` / ``user_agent`` / ``created_at`` for audit and future
  multi-device UX.
- Expired rows fail the active-check and force re-login.

This is the minimum Phase 0.C needs. Phase 7 may extend this with a full
middleware rewrite that moves session data fully out of the cookie.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class Session(BaseModel):
    """A row in the sessions table."""

    id: str
    user_id: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    ip: str | None = None
    user_agent: str | None = None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    def is_active(self, *, now: datetime | None = None) -> bool:
        """Return True if the session is neither revoked nor expired."""
        if self.revoked_at is not None:
            return False
        if now is None:
            now = datetime.now(tz=timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp > now
