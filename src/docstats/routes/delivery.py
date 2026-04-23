"""Delivery routes — Phase 9.A.

- ``POST /referrals/{id}/send`` — enqueue a delivery. Returns 303 to
  the referral detail page. Does NOT invoke the channel directly;
  the DB-backed dispatcher picks the row up on its next iteration.
- ``POST /referrals/{id}/deliveries/{delivery_id}/cancel`` — admin
  action (any authenticated user in scope) to kill a non-terminal
  delivery. Subsequent dispatcher iterations skip cancelled rows.

Phase 9.B adds share-token viewer routes in ``routes/share.py``.
Phase 9.E adds admin-level list/detail in ``routes/admin.py``.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Request
from fastapi.responses import RedirectResponse, Response

from docstats.delivery.base import ChannelDisabledError
from docstats.delivery.registry import CHANNEL_NAMES, enabled_channels, get_channel
from docstats.domain.audit import record as audit_record
from docstats.domain.deliveries import (
    CHANNEL_VALUES,
    IDEMPOTENCY_KEY_MAX_LENGTH,
    RECIPIENT_MAX_LENGTH,
)
from docstats.phi import require_phi_consent
from docstats.routes._common import get_scope
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/referrals", tags=["delivery"])


def _parse_include(value: str | None) -> list[str]:
    """Parse a comma-separated include spec into a clean list.

    Unknown / duplicate artifact tokens are silently dropped by the
    export layer at render time. We just normalize here so the
    packet_artifact JSON isn't an unbounded bag of garbage.
    """
    if not value:
        return []
    return [tok.strip() for tok in value.split(",") if tok.strip()]


@router.post("/{referral_id}/send")
async def send_referral(
    request: Request,
    referral_id: int = Path(..., ge=1),
    channel: str = Form(..., max_length=16),
    recipient: str = Form(..., max_length=RECIPIENT_MAX_LENGTH),
    include: str | None = Form(None, max_length=256),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    if channel not in CHANNEL_VALUES:
        raise HTTPException(status_code=422, detail=f"Unknown channel {channel!r}.")
    if channel not in CHANNEL_NAMES:  # defense in depth
        raise HTTPException(status_code=422, detail=f"Unknown channel {channel!r}.")

    recipient_clean = (recipient or "").strip()
    if not recipient_clean:
        raise HTTPException(status_code=422, detail="Recipient is required.")

    # Verify the referral exists in scope up front so the caller gets
    # a clean 404 (rather than a ValueError bubbling from storage).
    referral = storage.get_referral(scope, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found.")

    # Probe the channel registry so we fail fast with a 422 if the
    # channel is disabled (no env vars / no vendor impl yet). The
    # dispatcher will also check this path, but doing it here keeps
    # the UX tight — the user gets an immediate error instead of
    # watching a delivery flow to `failed` a few seconds later.
    try:
        get_channel(channel)
    except ChannelDisabledError as e:
        raise HTTPException(
            status_code=422,
            detail=f"Channel {channel!r} is not configured: {e}",
        )

    # Idempotency key — one per send-request. The channel may
    # overwrite it with a vendor-specific key before the actual send,
    # but having a unique key at enqueue time defends against
    # accidental double-submit from the browser.
    idempotency_key = f"{channel}:{secrets.token_hex(16)}"[:IDEMPOTENCY_KEY_MAX_LENGTH]

    packet_artifact: dict[str, object] = {"include": _parse_include(include)} if include else {}

    try:
        delivery = storage.create_delivery(
            scope,
            referral_id=referral_id,
            channel=channel,
            recipient=recipient_clean,
            packet_artifact=packet_artifact,
            idempotency_key=idempotency_key,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    audit_record(
        storage,
        action="delivery.create",
        request=request,
        actor_user_id=current_user["id"],
        scope_user_id=scope.user_id if scope.is_solo else None,
        scope_organization_id=scope.organization_id,
        entity_type="delivery",
        entity_id=str(delivery.id),
        metadata={
            "channel": channel,
            "referral_id": referral_id,
            "idempotency_key": idempotency_key,
        },
    )

    return RedirectResponse(f"/referrals/{referral_id}", status_code=303)


@router.post("/{referral_id}/deliveries/{delivery_id}/cancel")
async def cancel_delivery(
    request: Request,
    referral_id: int = Path(..., ge=1),
    delivery_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_phi_consent),
    scope: Scope = Depends(get_scope),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    delivery = storage.get_delivery(scope, delivery_id)
    if delivery is None or delivery.referral_id != referral_id:
        raise HTTPException(status_code=404, detail="Delivery not found.")

    cancelled = storage.cancel_delivery(scope, delivery_id, cancelled_by_user_id=current_user["id"])
    if cancelled:
        audit_record(
            storage,
            action="delivery.cancel",
            request=request,
            actor_user_id=current_user["id"],
            scope_user_id=scope.user_id if scope.is_solo else None,
            scope_organization_id=scope.organization_id,
            entity_type="delivery",
            entity_id=str(delivery_id),
            metadata={"referral_id": referral_id},
        )

    return RedirectResponse(f"/referrals/{referral_id}", status_code=303)


# Re-export for test convenience — tests can grep `enabled_channels` from
# here rather than reaching into the registry module.
_ = enabled_channels
