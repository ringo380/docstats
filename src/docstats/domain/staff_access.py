"""Staff access grant domain model."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel

DEFAULT_TTL_SECONDS: int = 24 * 3600  # 24 hours
TTL_OPTIONS: dict[str, int] = {
    "1 hour": 3600,
    "24 hours": 24 * 3600,
    "7 days": 7 * 24 * 3600,
}


class StaffAccessGrant(BaseModel):
    id: int
    user_id: int
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime

    def is_active(self, *, now: datetime | None = None) -> bool:
        t = now or datetime.now(tz=timezone.utc)
        return self.revoked_at is None and self.expires_at > t
