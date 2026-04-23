"""Outbound delivery domain model — Phase 9.

A Delivery represents one attempt (with retries) to send a rendered referral
packet to an external party via a Channel (fax / email / Direct Trust). The
row persists the full lifecycle from ``queued`` through ``delivered`` or
``failed`` so operators can triage stuck rows and the retry sweeper can
resume work across deploys.

Scope flows through the parent referral but is also DENORMALIZED onto each
row (``scope_user_id`` / ``scope_organization_id``) so admin list queries
(``GET /admin/deliveries``) don't need a join through ``referrals``. The
denormalization is enforced at the storage layer — see
``StorageBase.create_delivery``.

``idempotency_key`` is the vendor-webhook dedup primitive. Channel impls
generate it at enqueue time (typically ``f"{channel}:{uuid4()}"``) and
include it in the vendor-facing API call so that callback loops can resolve
back to a single delivery row even if the vendor re-delivers the same event
three times. See ``webhook_verifiers/`` for the per-vendor callback shape.

``packet_artifact`` is a JSON spec (``{"include": ["fax_cover", "summary"]}``)
describing which parts of the PDF packet to render. It's late-binding: the
dispatcher calls ``render_packet()`` at send time, NOT at enqueue time. If
attachments change between enqueue and dispatch, the dispatcher sends the
latest state. Real bytes-at-enqueue snapshotting lands in Phase 10 alongside
attachment file storage.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel

# ---- Enumerations (enforced at the DB CHECK layer; mirror the SQL) ----

CHANNEL_VALUES: Final[tuple[str, ...]] = ("fax", "email", "direct")

DELIVERY_STATUS_VALUES: Final[tuple[str, ...]] = (
    "queued",  # waiting for sweeper pickup
    "sending",  # dispatcher is actively calling the vendor
    "sent",  # vendor accepted; awaiting final delivery confirmation
    "delivered",  # vendor confirmed end-recipient receipt
    "failed",  # retries exhausted or fatal error
    "cancelled",  # admin cancelled before or during delivery
)

# Terminal statuses — no further state transitions. Sweeper skips them.
TERMINAL_DELIVERY_STATUSES: Final[frozenset[str]] = frozenset({"delivered", "failed", "cancelled"})

# Statuses the sweeper should pick up. "queued" rows were just enqueued;
# "sending" rows are stuck (a prior dispatcher crashed or got SIGTERM'd
# mid-send) and should be retried after a longer threshold.
PICKUP_DELIVERY_STATUSES: Final[frozenset[str]] = frozenset({"queued", "sending"})

ATTEMPT_RESULT_VALUES: Final[tuple[str, ...]] = (
    "in_progress",  # attempt started, not yet completed
    "success",  # vendor accepted; delivery advances to sent or delivered
    "retryable",  # transient failure (429, 5xx, timeout) — sweeper retries
    "fatal",  # non-recoverable (4xx with business-meaningful body) — no retry
)


# ---- Pydantic models ----


class Delivery(BaseModel):
    """A single outbound delivery attempt (with retries)."""

    id: int
    referral_id: int
    scope_user_id: int | None = None
    scope_organization_id: int | None = None
    channel: str  # must be in CHANNEL_VALUES
    recipient: str
    status: str = "queued"  # must be in DELIVERY_STATUS_VALUES
    vendor_name: str | None = None
    vendor_message_id: str | None = None
    idempotency_key: str | None = None
    packet_artifact: dict[str, Any] = {}
    retry_count: int = 0
    last_error_code: str | None = None
    last_error_message: str | None = None
    cancelled_at: datetime | None = None
    cancelled_by_user_id: int | None = None
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None = None
    delivered_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_DELIVERY_STATUSES


class DeliveryAttempt(BaseModel):
    """Per-retry row — operator-facing failure history."""

    id: int
    delivery_id: int
    attempt_number: int
    started_at: datetime
    completed_at: datetime | None = None
    result: str = "in_progress"  # must be in ATTEMPT_RESULT_VALUES
    error_code: str | None = None
    error_message: str | None = None  # truncated to 500 chars
    vendor_response_excerpt: str | None = None  # truncated to 2000 chars


# ---- Column caps (enforced at the storage layer) ----

ERROR_MESSAGE_MAX_LENGTH: Final[int] = 500
VENDOR_RESPONSE_EXCERPT_MAX_LENGTH: Final[int] = 2000
RECIPIENT_MAX_LENGTH: Final[int] = 320  # longest plausible email / Direct address
VENDOR_MESSAGE_ID_MAX_LENGTH: Final[int] = 255
IDEMPOTENCY_KEY_MAX_LENGTH: Final[int] = 128


def truncate_error_message(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:ERROR_MESSAGE_MAX_LENGTH]


def truncate_vendor_excerpt(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:VENDOR_RESPONSE_EXCERPT_MAX_LENGTH]
