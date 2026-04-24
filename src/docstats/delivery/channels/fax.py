"""Fax delivery channel via Documo — Phase 9.C.

Architecture
------------
Ships **feature-flagged**.  Real sends require Documo at the Professional
(BAA-signed) tier.  When ``DOCUMO_API_KEY`` is absent the channel factory
raises ``ChannelDisabledError``; the Send form hides fax automatically
until the user sets the env var.

Wire-level contract (confirmed with Documo docs):
  - ``POST https://api.documo.com/v1/fax/send``
  - ``Authorization: Basic <API_KEY>`` — per-Documo, the raw API key is
    sent in the Basic header (no base64 user:pass split).
  - Multipart/form-data with the fields:

      recipientFax   — E.164 number, e.g. ``+15555555555``
      recipientName  — optional display string
      coverPage      — ``true`` / ``false``; we default to ``false`` because
                       the ``fax_cover`` packet artifact from Phase 5.C is
                       already embedded in the submitted PDF.
      subject        — short subject line (lands on Documo cover page)
      notes          — optional long note
      files          — the binary attachment (application/pdf)

  - Response (2xx): JSON containing ``id`` (or ``faxId`` on some API
    versions) — we record whichever is present as ``vendor_message_id``.

Env vars
--------
``DOCUMO_API_KEY``            — required; absence → ``ChannelDisabledError``.
``DOCUMO_BASE_URL``           — optional override (defaults to prod).
``DOCUMO_COVER_PAGE_ENABLED`` — optional ``"true"`` to add Documo's cover
                                page on top of ours.  Not recommended.

Notes
-----
The dispatcher hands us ``packet_bytes`` — a PDF rendered upstream via
``exports/pdf.py::render_packet`` (Phase 5.C).  We never render PHI into
anything other than the attached PDF; the multipart subject/notes fields
are PHI-free strings pulled from ``packet_artifact``.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

import httpx

from docstats.delivery.base import ChannelDisabledError, DeliveryError, DeliveryReceipt

if TYPE_CHECKING:
    from docstats.domain.deliveries import Delivery

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.documo.com"
_SEND_PATH = "/v1/fax/send"
_TIMEOUT = 60.0  # fax uploads can be large


class DocumoFaxChannel:
    name = "fax"
    vendor_name = "Documo"

    def __init__(self) -> None:
        api_key = os.environ.get("DOCUMO_API_KEY", "")
        if not api_key:
            raise ChannelDisabledError("fax", reason="DOCUMO_API_KEY not set")
        self._api_key = api_key
        self._base_url = os.environ.get("DOCUMO_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
        self._cover_page = os.environ.get("DOCUMO_COVER_PAGE_ENABLED", "").lower() == "true"

    async def send(self, delivery: "Delivery", packet_bytes: bytes) -> DeliveryReceipt:
        if not packet_bytes:
            raise DeliveryError(
                "empty_packet",
                "Fax send requires a non-empty PDF packet",
                retryable=False,
            )

        artifact: dict[str, Any] = dict(delivery.packet_artifact or {})
        subject = str(artifact.get("fax_subject") or artifact.get("subject") or "Referral")
        notes = str(artifact.get("fax_notes") or artifact.get("notes") or "")
        recipient_name = str(artifact.get("recipient_name") or "")

        # Multipart body.  httpx quirk — pass the file tuple via ``files=``,
        # text fields via ``data=``; httpx will merge them into one multipart
        # body with the correct Content-Type boundary.
        data: dict[str, str] = {
            "recipientFax": delivery.recipient,
            "coverPage": "true" if self._cover_page else "false",
            "subject": subject[:120],
        }
        if recipient_name:
            data["recipientName"] = recipient_name[:120]
        if notes:
            data["notes"] = notes[:500]

        files = {
            "files": (
                f"referral-{delivery.referral_id}.pdf",
                packet_bytes,
                "application/pdf",
            )
        }

        headers: dict[str, str] = {
            "Authorization": f"Basic {self._api_key}",
            "Accept": "application/json",
        }
        # Documo honors per-request idempotency via client-supplied headers
        # on the Professional tier.  Defensive: only send if we have one.
        if delivery.idempotency_key:
            headers["Idempotency-Key"] = delivery.idempotency_key

        url = f"{self._base_url}{_SEND_PATH}"
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            try:
                resp = await client.post(url, headers=headers, data=data, files=files)
            except httpx.TimeoutException as exc:
                raise DeliveryError("timeout", str(exc), retryable=True) from exc
            except httpx.RequestError as exc:
                raise DeliveryError("network_error", str(exc), retryable=True) from exc

        if resp.status_code == 429:
            raise DeliveryError("rate_limited", "Documo rate limit", retryable=True)
        if resp.status_code >= 500:
            raise DeliveryError(
                "vendor_5xx",
                f"Documo {resp.status_code}: {resp.text[:200]}",
                retryable=True,
            )
        if resp.status_code not in (200, 201, 202):
            raise DeliveryError(
                "vendor_4xx",
                f"Documo {resp.status_code}: {resp.text[:200]}",
                retryable=False,
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise DeliveryError(
                "vendor_bad_response",
                f"Documo returned non-JSON: {resp.text[:200]}",
                retryable=False,
            ) from exc

        # Documo's field naming varies slightly across tiers: ``id`` is
        # modern, ``faxId`` / ``fax_id`` appear on legacy accounts.
        message_id = body.get("id") or body.get("faxId") or body.get("fax_id") or ""
        if not message_id:
            raise DeliveryError(
                "vendor_bad_response",
                "Documo response missing fax id",
                retryable=False,
            )

        return DeliveryReceipt(
            vendor_name=self.vendor_name,
            vendor_message_id=str(message_id),
            status="sent",
            vendor_response_excerpt=f"id={message_id}",
        )

    async def poll_status(self, delivery: "Delivery") -> None:
        # Documo pushes status via webhook; polling is not used.  The
        # sweeper's stuck-sending guard (Phase 9.A) still protects us if a
        # webhook is dropped — rows come back into ``queued`` after the
        # threshold.  A real poll implementation could hit
        # ``GET /v1/fax/{id}`` but we defer it to Phase 9.E hardening.
        return None
