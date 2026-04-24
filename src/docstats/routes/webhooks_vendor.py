"""Vendor-initiated webhook callbacks — Phase 9.B/9.C.

Each vendor route:
1. Reads the raw body BEFORE consuming it through the framework
2. Verifies the HMAC signature (vendor-specific verifier in ``webhook_verifiers/``)
3. Records the raw payload in ``webhook_inbox`` for dead-letter recovery
4. Resolves the delivery via ``vendor_message_id``
5. Updates the delivery status idempotently

Resend (Phase 9.B)
------------------
POST /webhooks/resend
Uses Svix-format HMAC (``svix-id``, ``svix-timestamp``, ``svix-signature`` headers).
Event types we act on: ``email.sent`` → ``sent``, ``email.delivered`` → ``delivered``,
``email.bounced`` / ``email.complained`` → ``failed``.
Secret: ``RESEND_WEBHOOK_SECRET`` (format: ``whsec_...``).

Documo (Phase 9.C)
------------------
POST /webhooks/documo
Uses HMAC-SHA256 over raw body (``X-Documo-Signature: sha256=<hex>``).
Event types we act on:
  ``fax.queued`` / ``fax.sending``   → ``sending``
  ``fax.sent``                        → ``sent``
  ``fax.delivered``                   → ``delivered``
  ``fax.failed`` / ``fax.cancelled``  → ``failed``
Secret: ``DOCUMO_WEBHOOK_SECRET``.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from docstats.domain.deliveries import TERMINAL_DELIVERY_STATUSES
from docstats.storage import get_storage
from docstats.storage_base import StorageBase
from docstats.webhook_verifiers.documo import DocumoVerificationError, verify_documo
from docstats.webhook_verifiers.svix import SvixVerificationError, verify_svix

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_RESEND_STATUS_MAP: dict[str, str] = {
    "email.sent": "sent",
    "email.delivered": "delivered",
    "email.bounced": "failed",
    "email.complained": "failed",
}

_RESEND_ERROR_CODES: dict[str, str] = {
    "email.bounced": "email_bounced",
    "email.complained": "email_complained",
}

_DOCUMO_STATUS_MAP: dict[str, str] = {
    "fax.queued": "sending",
    "fax.sending": "sending",
    "fax.sent": "sent",
    "fax.delivered": "delivered",
    "fax.failed": "failed",
    "fax.cancelled": "failed",
}

_DOCUMO_ERROR_CODES: dict[str, str] = {
    "fax.failed": "fax_failed",
    "fax.cancelled": "fax_cancelled",
}

_ALLOWED_HEADERS = frozenset(
    {
        "content-type",
        "svix-id",
        "svix-timestamp",
        "svix-signature",
        "user-agent",
        "x-documo-signature",
        "x-documo-timestamp",
    }
)


def _filter_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items() if k.lower() in _ALLOWED_HEADERS}


@router.post("/resend")
async def resend_webhook(
    request: Request,
    storage: StorageBase = Depends(get_storage),
) -> JSONResponse:
    secret = os.environ.get("RESEND_WEBHOOK_SECRET", "")
    body = await request.body()

    if secret:
        try:
            verify_svix(dict(request.headers), body, secret)
        except SvixVerificationError as exc:
            logger.warning("Resend webhook signature invalid: %s", exc)
            raise HTTPException(status_code=400, detail="Invalid signature.")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    filtered_headers = _filter_headers(dict(request.headers))
    try:
        storage.record_inbound_webhook(
            source="resend",
            payload_json=payload,
            http_headers_json=filtered_headers,
            signature=request.headers.get("svix-signature"),
            status="received",
        )
    except Exception:
        logger.exception("Failed to record Resend webhook in inbox")

    event_type: str = payload.get("type", "")
    new_status = _RESEND_STATUS_MAP.get(event_type)
    if new_status is None:
        # Unknown / uninteresting event — acknowledge silently
        return JSONResponse({"ok": True, "action": "ignored"})

    # Resend embeds the message ID in data.email_id (older format) or data.id
    data = payload.get("data", {})
    vendor_message_id: str = data.get("email_id") or data.get("id") or ""
    if not vendor_message_id:
        logger.warning("Resend webhook missing message ID for event %s", event_type)
        return JSONResponse({"ok": True, "action": "no_message_id"})

    delivery = storage.get_delivery_by_vendor_message_id(vendor_message_id)
    if delivery is None:
        logger.info(
            "Resend webhook for unknown vendor_message_id %r (event %s)",
            vendor_message_id,
            event_type,
        )
        return JSONResponse({"ok": True, "action": "unknown_delivery"})

    if delivery.status in TERMINAL_DELIVERY_STATUSES:
        return JSONResponse({"ok": True, "action": "already_terminal"})

    error_code = _RESEND_ERROR_CODES.get(event_type)
    error_message = data.get("reason") or data.get("description")

    if new_status == "sent":
        storage.mark_delivery_sent(
            delivery.id,
            vendor_name="Resend",
            vendor_message_id=vendor_message_id,
        )
    elif new_status == "delivered":
        storage.mark_delivery_sent(
            delivery.id,
            vendor_name="Resend",
            vendor_message_id=vendor_message_id,
            status="delivered",
        )
    elif new_status == "failed":
        storage.mark_delivery_failed(
            delivery.id,
            error_code=error_code or "vendor_error",
            error_message=str(error_message or "")[:500],
        )

    logger.info(
        "Resend webhook: delivery %s → %s (event=%s)",
        delivery.id,
        new_status,
        event_type,
    )
    return JSONResponse({"ok": True, "action": "updated", "delivery_id": delivery.id})


@router.post("/documo")
async def documo_webhook(
    request: Request,
    storage: StorageBase = Depends(get_storage),
) -> JSONResponse:
    secret = os.environ.get("DOCUMO_WEBHOOK_SECRET", "")
    body = await request.body()

    if secret:
        try:
            verify_documo(dict(request.headers), body, secret)
        except DocumoVerificationError as exc:
            logger.warning("Documo webhook signature invalid: %s", exc)
            raise HTTPException(status_code=400, detail="Invalid signature.")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    filtered_headers = _filter_headers(dict(request.headers))
    try:
        storage.record_inbound_webhook(
            source="documo",
            payload_json=payload,
            http_headers_json=filtered_headers,
            signature=request.headers.get("x-documo-signature"),
            status="received",
        )
    except Exception:
        logger.exception("Failed to record Documo webhook in inbox")

    event_type: str = payload.get("event") or payload.get("type") or ""
    new_status = _DOCUMO_STATUS_MAP.get(event_type)
    if new_status is None:
        return JSONResponse({"ok": True, "action": "ignored"})

    # Documo payload shape varies slightly across tiers; accept both
    # ``data.id`` / ``data.faxId`` and top-level ``faxId``.
    data = payload.get("data", payload) if isinstance(payload.get("data"), dict) else payload
    vendor_message_id: str = data.get("id") or data.get("faxId") or data.get("fax_id") or ""
    if not vendor_message_id:
        logger.warning("Documo webhook missing fax id for event %s", event_type)
        return JSONResponse({"ok": True, "action": "no_message_id"})

    delivery = storage.get_delivery_by_vendor_message_id(str(vendor_message_id))
    if delivery is None:
        logger.info(
            "Documo webhook for unknown vendor_message_id %r (event %s)",
            vendor_message_id,
            event_type,
        )
        return JSONResponse({"ok": True, "action": "unknown_delivery"})

    if delivery.status in TERMINAL_DELIVERY_STATUSES:
        return JSONResponse({"ok": True, "action": "already_terminal"})

    error_code = _DOCUMO_ERROR_CODES.get(event_type)
    error_message = data.get("reason") or data.get("errorReason") or data.get("description")

    if new_status == "sending":
        # Informational — the dispatcher owns queued→sending and the
        # vendor_message_id was written when our channel.send() returned.
        # We don't want to clobber sent_at, so skip the write and let the
        # terminal-state webhook (sent / delivered / failed) resolve the row.
        logger.info(
            "Documo sending webhook for delivery %s (event=%s) — informational",
            delivery.id,
            event_type,
        )
        return JSONResponse({"ok": True, "action": "informational"})
    elif new_status == "sent":
        storage.mark_delivery_sent(
            delivery.id,
            vendor_name="Documo",
            vendor_message_id=str(vendor_message_id),
        )
    elif new_status == "delivered":
        storage.mark_delivery_sent(
            delivery.id,
            vendor_name="Documo",
            vendor_message_id=str(vendor_message_id),
            status="delivered",
        )
    elif new_status == "failed":
        storage.mark_delivery_failed(
            delivery.id,
            error_code=error_code or "vendor_error",
            error_message=str(error_message or "")[:500],
        )

    logger.info(
        "Documo webhook: delivery %s → %s (event=%s)",
        delivery.id,
        new_status,
        event_type,
    )
    return JSONResponse({"ok": True, "action": "updated", "delivery_id": delivery.id})
