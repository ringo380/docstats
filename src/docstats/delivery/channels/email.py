"""Email delivery channel via Resend — Phase 9.B.

Architecture
------------
PHI never enters the email body.  The email carries:
  - Subject: "Referral from <org>" (no patient name)
  - A short intro sentence
  - A signed share-token URL that opens a 2FA-gated viewer

``RESEND_API_KEY`` required; absent → ``ChannelDisabledError``.
``RESEND_FROM_ADDRESS`` optional, defaults to "referme.help <no-reply@referme.help>".
``SHARE_TOKEN_BASE_URL`` optional, defaults to "https://referme.help".

The caller (dispatcher) must supply ``delivery.recipient`` as a valid
email address and ``delivery.packet_artifact.get("share_token_url")``
as the pre-generated viewer URL — see ``routes/delivery.py`` for the
token-generation flow.
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

_DEFAULT_FROM = "referme.help <no-reply@referme.help>"
_RESEND_SEND_URL = "https://api.resend.com/emails"
_TIMEOUT = 30.0


class ResendEmailChannel:
    name = "email"
    vendor_name = "Resend"

    def __init__(self) -> None:
        api_key = os.environ.get("RESEND_API_KEY", "")
        if not api_key:
            raise ChannelDisabledError("email", reason="RESEND_API_KEY not set")
        self._api_key = api_key
        self._from_address = os.environ.get("RESEND_FROM_ADDRESS", _DEFAULT_FROM)

    async def send(self, delivery: "Delivery", packet_bytes: bytes) -> DeliveryReceipt:
        share_url = delivery.packet_artifact.get("share_token_url", "")
        if not share_url:
            raise DeliveryError(
                "missing_share_url",
                "Delivery packet_artifact is missing share_token_url",
                retryable=False,
            )

        subject = delivery.packet_artifact.get("email_subject", "You have a referral to review")
        body_html = _build_html(share_url, delivery.packet_artifact)
        body_text = _build_text(share_url, delivery.packet_artifact)

        payload: dict[str, Any] = {
            "from": self._from_address,
            "to": [delivery.recipient],
            "subject": subject,
            "html": body_html,
            "text": body_text,
        }
        idempotency_key = delivery.idempotency_key
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            try:
                resp = await client.post(_RESEND_SEND_URL, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                raise DeliveryError("timeout", str(exc), retryable=True) from exc
            except httpx.RequestError as exc:
                raise DeliveryError("network_error", str(exc), retryable=True) from exc

        if resp.status_code == 429:
            raise DeliveryError("rate_limited", "Resend rate limit", retryable=True)
        if resp.status_code >= 500:
            raise DeliveryError(
                "vendor_5xx",
                f"Resend {resp.status_code}: {resp.text[:200]}",
                retryable=True,
            )
        if resp.status_code not in (200, 201):
            raise DeliveryError(
                "vendor_4xx",
                f"Resend {resp.status_code}: {resp.text[:200]}",
                retryable=False,
            )

        data = resp.json()
        message_id: str = data.get("id", "")
        return DeliveryReceipt(
            vendor_name=self.vendor_name,
            vendor_message_id=message_id,
            status="sent",
            vendor_response_excerpt=f"id={message_id}",
        )

    async def poll_status(self, delivery: "Delivery") -> None:
        # Resend uses webhook callbacks; polling not supported.
        return None


def _build_html(share_url: str, artifact: dict[str, Any]) -> str:
    org_name = artifact.get("org_name", "Your care team")
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Referral notification</title></head>
<body style="font-family:sans-serif;color:#1a1a2e;max-width:600px;margin:0 auto;padding:24px">
  <h2 style="color:#4361ee">You have a referral to review</h2>
  <p>{org_name} has sent you a referral packet for review.</p>
  <p>Click the button below to view the referral details. You will be asked to
  verify your identity before any clinical information is displayed.</p>
  <p style="margin:32px 0">
    <a href="{share_url}"
       style="background:#4361ee;color:#fff;padding:12px 24px;border-radius:6px;
              text-decoration:none;font-weight:600">
      View referral
    </a>
  </p>
  <p style="font-size:12px;color:#666">
    This link expires in 7 days.  If you did not expect this referral, you can
    safely ignore this email.
  </p>
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0">
  <p style="font-size:11px;color:#999">
    Sent via referme.help &mdash; secure referral management
  </p>
</body>
</html>"""


def _build_text(share_url: str, artifact: dict[str, Any]) -> str:
    org_name = artifact.get("org_name", "Your care team")
    return (
        f"{org_name} has sent you a referral packet for review.\n\n"
        f"View referral: {share_url}\n\n"
        "You will be asked to verify your identity before any clinical "
        "information is displayed. This link expires in 7 days.\n\n"
        "If you did not expect this referral, you can safely ignore this email."
    )
