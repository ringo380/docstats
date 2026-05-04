"""Direct Trust delivery channel — Phase 9.D scaffolding.

This is a vendor-agnostic skeleton that sits idle until a HISP contract
activates it. Activation = setting the four required env vars on Railway:

  - ``DIRECT_HISP_USERNAME``
  - ``DIRECT_HISP_PASSWORD``
  - ``DIRECT_HISP_ENDPOINT``      (vendor's send-message URL)
  - ``DIRECT_HISP_FROM_ADDRESS``  (the org's Direct address)

Optional:

  - ``DIRECT_HISP_VENDOR``        (cosmetic — appears in receipts/logs;
                                   defaults to ``"DirectTrust HISP"``)
  - ``DIRECT_HISP_AUTH_SCHEME``   (``basic`` (default) or ``bearer``)

The shape mirrors :class:`docstats.delivery.channels.fax.DocumoFaxChannel`
intentionally: REST POST with multipart body (``to``, ``from``, ``subject``,
PDF as attachment), Basic auth header, idempotency-key passthrough, and
error classification matching Phase 9.E conventions
(timeout/network/429/5xx → retryable; 4xx → fatal).

The factory in ``delivery/registry.py`` keeps returning ``ChannelDisabledError``
until the four env vars are set — see ``_direct_channel`` there. When a
vendor is picked, the only changes required are:

  1. Confirm the multipart form-field names this skeleton uses match what
     the vendor expects (DataMotion uses ``to``/``from``/``subject``/``files``;
     others may vary). Adjust ``_build_form_data`` if needed.
  2. Land vendor-specific webhook signature verification in
     ``webhook_verifiers/direct.py`` (does not exist yet — gated on the
     vendor pick).
  3. Switch the registry factory from the disabled stub to ``DirectTrustChannel()``.

Per CLAUDE.md "no PHI in logs" rule, the channel never logs the recipient
address or packet bytes — only vendor message ids and HTTP status codes.
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

_TIMEOUT = 60.0  # Direct messages route through SMTP under the HISP; allow longer than email/fax
_DEFAULT_VENDOR_NAME = "DirectTrust HISP"
_REQUIRED_ENV_VARS = (
    "DIRECT_HISP_USERNAME",
    "DIRECT_HISP_PASSWORD",
    "DIRECT_HISP_ENDPOINT",
    "DIRECT_HISP_FROM_ADDRESS",
)


class DirectTrustChannel:
    """Generic Direct Trust REST client.

    Concrete vendor selection happens at deploy time via env vars. The
    channel itself is vendor-agnostic; per-vendor quirks (response
    field names, webhook signature schemes) live in adapters layered
    on top of this class once a vendor is picked.
    """

    name = "direct"

    def __init__(self) -> None:
        missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
        if missing:
            raise ChannelDisabledError(
                "direct",
                reason=f"missing env vars: {', '.join(missing)}",
            )
        self._username = os.environ["DIRECT_HISP_USERNAME"]
        self._password = os.environ["DIRECT_HISP_PASSWORD"]
        self._endpoint = os.environ["DIRECT_HISP_ENDPOINT"]
        self._from_address = os.environ["DIRECT_HISP_FROM_ADDRESS"]
        self._auth_scheme = os.environ.get("DIRECT_HISP_AUTH_SCHEME", "basic").lower()
        self.vendor_name = os.environ.get("DIRECT_HISP_VENDOR", _DEFAULT_VENDOR_NAME)

    async def send(self, delivery: "Delivery", packet_bytes: bytes) -> DeliveryReceipt:
        if not packet_bytes:
            raise DeliveryError(
                "empty_packet",
                "Direct Trust send received an empty packet",
                retryable=False,
            )

        subject = delivery.packet_artifact.get("direct_subject", "Referral packet")
        body_text = delivery.packet_artifact.get(
            "direct_body",
            "Please see the attached referral packet.",
        )
        attachment_name = delivery.packet_artifact.get(
            "direct_attachment_filename",
            f"referral-{delivery.referral_id}.pdf",
        )

        data = _build_form_data(
            from_address=self._from_address,
            to_address=delivery.recipient,
            subject=subject,
            body_text=body_text,
        )
        files = {"files": (attachment_name, packet_bytes, "application/pdf")}
        headers = _build_headers(
            scheme=self._auth_scheme,
            username=self._username,
            password=self._password,
            idempotency_key=delivery.idempotency_key,
        )

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            try:
                resp = await client.post(
                    self._endpoint,
                    data=data,
                    files=files,
                    headers=headers,
                )
            except httpx.TimeoutException as exc:
                raise DeliveryError("timeout", str(exc), retryable=True) from exc
            except httpx.RequestError as exc:
                raise DeliveryError("network_error", str(exc), retryable=True) from exc

        return self._receipt_from_response(resp)

    async def poll_status(self, delivery: "Delivery") -> None:
        # Most HISPs use webhook callbacks for status. Polling support
        # lands when a vendor adds it; for now, rely on webhooks.
        return None

    def _receipt_from_response(self, resp: httpx.Response) -> DeliveryReceipt:
        if resp.status_code == 429:
            raise DeliveryError("rate_limited", "HISP rate limit", retryable=True)
        if resp.status_code >= 500:
            raise DeliveryError(
                "vendor_5xx",
                f"HISP {resp.status_code}: {resp.text[:200]}",
                retryable=True,
            )
        if resp.status_code not in (200, 201, 202):
            raise DeliveryError(
                "vendor_4xx",
                f"HISP {resp.status_code}: {resp.text[:200]}",
                retryable=False,
            )

        try:
            data: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise DeliveryError(
                "malformed_response",
                f"HISP returned non-JSON: {resp.text[:200]}",
                retryable=False,
            ) from exc

        message_id = _extract_message_id(data)
        if not message_id:
            raise DeliveryError(
                "missing_message_id",
                f"HISP response missing message id: {str(data)[:200]}",
                retryable=False,
            )
        return DeliveryReceipt(
            vendor_name=self.vendor_name,
            vendor_message_id=message_id,
            status="sent",
            vendor_response_excerpt=f"id={message_id}",
        )


def _build_form_data(
    *, from_address: str, to_address: str, subject: str, body_text: str
) -> dict[str, str]:
    """Vendor-agnostic multipart fields.

    DataMotion-style payload. If a different vendor wins, override the
    field names here in the activation PR.
    """
    return {
        "from": from_address,
        "to": to_address,
        "subject": subject,
        "body": body_text,
    }


def _build_headers(
    *, scheme: str, username: str, password: str, idempotency_key: str | None
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if scheme == "bearer":
        # Some HISPs use "Bearer <token>" — pack username only as the token.
        headers["Authorization"] = f"Bearer {username}"
    else:
        # Default: HTTP Basic. Build the Authorization header explicitly so
        # httpx multipart encoding stays untouched.
        import base64

        creds = f"{username}:{password}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(creds).decode("ascii")
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _extract_message_id(data: dict[str, Any]) -> str | None:
    """Look for the vendor's message-id field under common names."""
    for key in ("id", "messageId", "message_id", "messageID", "directMessageId"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None
