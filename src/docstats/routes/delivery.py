"""Delivery routes — Phase 9.A/9.B.

- ``POST /referrals/{id}/send`` — enqueue a delivery. Returns 303 to
  the referral detail page. Does NOT invoke the channel directly;
  the DB-backed dispatcher picks the row up on its next iteration.
  For email deliveries a share token is generated and stored in
  ``packet_artifact["share_token_url"]`` so the dispatcher can embed
  the link in the email without a PHI lookup.
- ``POST /referrals/{id}/deliveries/{delivery_id}/cancel`` — admin
  action (any authenticated user in scope) to kill a non-terminal
  delivery. Subsequent dispatcher iterations skip cancelled rows.

Phase 9.E adds admin-level list/detail in ``routes/admin.py``.
"""

from __future__ import annotations

import logging
import os
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
from docstats.domain.share_tokens import (
    generate_token,
    hash_second_factor,
    hash_token,
    token_expires_at,
)
from docstats.phi import require_phi_consent
from docstats.routes._common import get_scope
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase
from docstats.validators import ValidationError, validate_fax_number

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

    # Channel-specific recipient normalization.  Fax numbers get E.164'd so
    # the storage row + any downstream retry carries the canonical form.
    if channel == "fax":
        try:
            recipient_clean = validate_fax_number(recipient_clean)
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=str(e))

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

    # For email deliveries, pre-generate the share token URL so it can be
    # embedded in packet_artifact at enqueue time. The share_token DB row is
    # persisted after delivery creation (we need delivery.id as FK). If token
    # creation fails the delivery still exists — dispatcher will find no
    # share_token_url and fail fast with error_code="missing_share_url".
    share_token_plaintext: str | None = None
    share_token_kwargs: dict[str, object] = {}
    email_packet_extras: dict[str, object] = {}
    if channel == "email" and os.environ.get("SHARE_TOKEN_SECRET"):
        patient = storage.get_patient(scope, referral.patient_id) if referral.patient_id else None
        second_factor_kind = "none"
        second_factor_hash: str | None = None
        if patient and patient.date_of_birth:
            try:
                second_factor_hash = hash_second_factor(patient.date_of_birth)
                second_factor_kind = "patient_dob"
            except ValueError:
                pass
        share_token_plaintext = generate_token()
        base_url = os.environ.get("SHARE_TOKEN_BASE_URL", "https://referme.help").rstrip("/")
        share_url = f"{base_url}/share/{share_token_plaintext}"
        org = storage.get_organization(scope.organization_id) if scope.organization_id else None
        share_token_kwargs = {
            "token_hash": hash_token(share_token_plaintext),
            "expires_at": token_expires_at(),
            "second_factor_kind": second_factor_kind,
            "second_factor_hash": second_factor_hash,
        }
        email_packet_extras = {
            "share_token_url": share_url,
            "email_subject": "You have a referral to review",
            "org_name": org.name if org else "Your care team",
        }

    packet_artifact: dict[str, object] = {
        **({"include": _parse_include(include)} if include else {}),
        **email_packet_extras,
    }

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

    if share_token_plaintext and share_token_kwargs:
        try:
            storage.create_share_token(delivery_id=delivery.id, **share_token_kwargs)  # type: ignore[arg-type]
        except Exception:
            logger.exception(
                "Share token persistence failed for delivery %s; email will lack link",
                delivery.id,
            )

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
