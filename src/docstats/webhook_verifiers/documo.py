"""Documo webhook signature verification — Phase 9.C.

Documo signs outbound webhooks using HMAC-SHA256 over the raw request body.
Contract (documented on the Documo dashboard, confirmed on BAA onboarding):

  - Header ``X-Documo-Signature``: ``sha256=<hex digest>``
    (bare hex is also accepted for tooling that strips the ``sha256=`` prefix)
  - Optional header ``X-Documo-Timestamp``: unix seconds, checked against
    ``tolerance_seconds`` to guard against replay.  Absent → no replay check
    (Documo does not emit timestamps on legacy webhooks).
  - Secret: the ``DOCUMO_WEBHOOK_SECRET`` env var, raw string bytes.

Signature spec is deliberately generic — Documo's exact scheme is subject
to tightening once the first live webhook lands post-BAA.  If Documo
announces a spec change, this verifier is the only place that needs to
adapt; the route layer (``/webhooks/documo``) is agnostic.

Usage::

    from docstats.webhook_verifiers.documo import verify_documo

    body = await request.body()
    try:
        verify_documo(request.headers, body, secret)
    except DocumoVerificationError as exc:
        raise HTTPException(400, detail=str(exc))
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Mapping


class DocumoVerificationError(Exception):
    """Raised when a Documo-signed webhook fails verification."""


def verify_documo(
    headers: Mapping[str, str],
    body: bytes,
    secret: str,
    *,
    tolerance_seconds: int = 300,
) -> None:
    """Verify a Documo-signed webhook.  Raises :class:`DocumoVerificationError`.

    ``secret`` is the ``DOCUMO_WEBHOOK_SECRET`` value.  Signature header is
    ``X-Documo-Signature`` (case-insensitive lookup).  If ``X-Documo-Timestamp``
    is present, its value is replay-checked against ``tolerance_seconds``.
    """
    sig_header = headers.get("x-documo-signature") or headers.get("X-Documo-Signature") or ""
    if not sig_header:
        raise DocumoVerificationError("Missing X-Documo-Signature header")

    # Optional replay guard.
    ts_header = headers.get("x-documo-timestamp") or headers.get("X-Documo-Timestamp") or ""
    if ts_header:
        try:
            ts = int(ts_header)
        except ValueError as exc:
            raise DocumoVerificationError("X-Documo-Timestamp is not an integer") from exc
        now = int(time.time())
        if abs(now - ts) > tolerance_seconds:
            raise DocumoVerificationError(
                f"X-Documo-Timestamp {ts_header!r} outside ±{tolerance_seconds}s window"
            )

    if not secret:
        raise DocumoVerificationError("DOCUMO_WEBHOOK_SECRET not configured")

    expected_hex = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    provided = sig_header.strip()
    if provided.lower().startswith("sha256="):
        provided = provided.split("=", 1)[1]

    if not hmac.compare_digest(expected_hex, provided.lower()):
        raise DocumoVerificationError("Documo signature mismatch")
