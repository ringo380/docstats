"""Append-only audit log primitive.

Every mutation that changes user, patient, or referral state should record an
``AuditEvent`` via :func:`record`. The log is append-only by contract; there is
no update path in either storage backend. Recording failures are logged and
swallowed so audit-log issues never break user-facing flows.

Action vocabulary is a dotted verb phrase: ``{entity}.{verb}``. Current actions:

- ``user.login`` — successful password login
- ``user.login_failed`` — failed password login (no user_id)
- ``user.login_github`` — successful GitHub OAuth
- ``user.signup`` — new account created
- ``user.logout`` — session ended
- ``user.terms_accepted`` — onboarding terms acceptance
- ``provider.save`` — NPI added to the user's rolodex
- ``provider.unsave`` — NPI removed from the rolodex

Future phases extend this list; keep the vocabulary documented here as it grows.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from fastapi import Request

    from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)


class AuditEvent(BaseModel):
    """A single append-only audit log row."""

    id: int
    actor_user_id: int | None = None
    scope_user_id: int | None = None
    scope_organization_id: int | None = None
    action: str
    entity_type: str | None = None
    entity_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    ip: str | None = None
    user_agent: str | None = None
    created_at: datetime


def client_ip(request: "Request") -> str | None:
    """Extract the client IP honoring ``X-Forwarded-For``.

    Railway proxies requests, so ``request.client.host`` alone is the proxy's
    address. The left-most entry in ``X-Forwarded-For`` is the originating
    client per RFC 7239 convention.
    """
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip() or None
    if request.client:
        return request.client.host
    return None


def record(
    storage: "StorageBase",
    *,
    action: str,
    request: "Request | None" = None,
    actor_user_id: int | None = None,
    scope_user_id: int | None = None,
    scope_organization_id: int | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record an audit event; never raises.

    Pass ``request`` to auto-populate ``ip`` and ``user_agent``. If omitted,
    both default to ``None`` (useful for CLI / background jobs).
    """
    ip: str | None = None
    user_agent: str | None = None
    if request is not None:
        ip = client_ip(request)
        ua = request.headers.get("User-Agent")
        user_agent = ua[:500] if ua else None

    try:
        storage.record_audit_event(
            action=action,
            actor_user_id=actor_user_id,
            scope_user_id=scope_user_id,
            scope_organization_id=scope_organization_id,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata=metadata,
            ip=ip,
            user_agent=user_agent,
        )
    except Exception:
        logger.exception("Failed to record audit event action=%s", action)
