"""Svix-format webhook signature verification (used by Resend).

Resend signs outbound webhooks using Svix's algorithm:
  - Headers: ``svix-id``, ``svix-timestamp``, ``svix-signature``
  - Signed message: ``{svix-id}.{svix-timestamp}.{raw_body_bytes}``
  - Signature: base64-encoded HMAC-SHA256 of the signed message

Replay guard: ``svix-timestamp`` must be within ±5 minutes of server
time.  A missing / wrong signature raises :class:`SvixVerificationError`.
``RESEND_WEBHOOK_SECRET`` must start with ``"whsec_"``; the base64-decoded
bytes are the HMAC key.

Usage::

    from docstats.webhook_verifiers.svix import verify_svix

    payload_bytes = await request.body()
    try:
        verify_svix(request.headers, payload_bytes, secret)
    except SvixVerificationError as exc:
        raise HTTPException(400, detail=str(exc))
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Mapping


class SvixVerificationError(Exception):
    """Raised when a Svix-signed webhook fails verification."""


def verify_svix(
    headers: Mapping[str, str],
    body: bytes,
    secret: str,
    *,
    tolerance_seconds: int = 300,
) -> None:
    """Verify a Svix-signed webhook.  Raises :class:`SvixVerificationError` on failure.

    ``secret`` is the ``whsec_…`` string from the Svix/Resend dashboard.
    """
    msg_id = headers.get("svix-id") or headers.get("Svix-Id", "")
    timestamp = headers.get("svix-timestamp") or headers.get("Svix-Timestamp", "")
    sig_header = headers.get("svix-signature") or headers.get("Svix-Signature", "")

    if not msg_id or not timestamp or not sig_header:
        raise SvixVerificationError("Missing svix-id / svix-timestamp / svix-signature headers")

    # Replay guard
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise SvixVerificationError("svix-timestamp is not an integer") from exc
    now = int(time.time())
    if abs(now - ts) > tolerance_seconds:
        raise SvixVerificationError(
            f"svix-timestamp {timestamp!r} is outside ±{tolerance_seconds}s window"
        )

    # Decode key
    raw_secret = secret
    if raw_secret.startswith("whsec_"):
        raw_secret = raw_secret[len("whsec_") :]
    try:
        key = base64.b64decode(raw_secret)
    except Exception as exc:
        raise SvixVerificationError("RESEND_WEBHOOK_SECRET is not valid base64") from exc

    # Compute expected signature
    signed_content = f"{msg_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed_content, hashlib.sha256).digest()).decode()

    # sig_header may carry multiple space-separated "v1,<sig>" tokens
    provided = [tok.split(",", 1)[1] for tok in sig_header.split(" ") if "," in tok]
    if not any(hmac.compare_digest(expected, sig) for sig in provided):
        raise SvixVerificationError("Svix signature mismatch")
