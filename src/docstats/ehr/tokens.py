"""SMART-on-FHIR token refresh helper, shared by routes and background tasks.

Previously lived as `_maybe_refresh` in ``docstats.routes.ehr``, but the
issue-#157 status poller in ``docstats.ehr.status_poller`` needs the same
helper. Importing across the routes → ehr direction inverts the layer
boundary, so the helper now lives in ``ehr/`` where both callers can reach it.

The helper is sync. Callers from async code (route handlers, the poller)
wrap calls in ``loop.run_in_executor``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from cryptography.fernet import InvalidToken

from docstats.domain import audit
from docstats.domain.ehr import EHRConnection
from docstats.ehr import registry as _registry
from docstats.ehr.crypto import EHRConfigError, decrypt_token, encrypt_token

logger = logging.getLogger(__name__)

# Refresh tokens this many seconds before expiry. 60s gives the request
# itself time to complete before the access token actually expires.
_REFRESH_LEAD_SECONDS = 60


def maybe_refresh(conn: EHRConnection, storage) -> str:
    """Return a fresh plaintext access token, refreshing if needed.

    Dispatches to the correct vendor module via the registry. On refresh
    failure, returns the stale token so callers can proceed (the EHR will
    return 401). Never raises.

    Org-scoped JWT-bearer vendors (Redox) don't persist tokens; this helper
    is only meaningful for SMART-on-FHIR vendors that do. Returns "" when
    called against a token-less connection so callers fail closed.
    """
    if conn.access_token_enc is None:
        return ""
    try:
        access_token = decrypt_token(conn.access_token_enc)
    except (EHRConfigError, InvalidToken):
        logger.exception("Failed to decrypt EHR access token for connection_id=%d", conn.id)
        return ""

    if conn.refresh_token_enc is None:
        return access_token
    if conn.expires_at is None:
        # No expiry recorded — can't decide whether to refresh; trust the
        # stored token and let upstream 401s drive the next refresh attempt.
        return access_token

    now = datetime.now(tz=timezone.utc)
    if (conn.expires_at - now).total_seconds() > _REFRESH_LEAD_SECONDS:
        return access_token

    vendor = conn.ehr_vendor
    try:
        vendor_mod = _registry.get(vendor)
    except ValueError:
        logger.error("Unknown EHR vendor %r for connection_id=%d", vendor, conn.id)
        return access_token

    try:
        refresh_token = decrypt_token(conn.refresh_token_enc)
        # Pass iss_override so multi-tenant vendors (eCW; future Epic/Cerner
        # multi-tenant) refresh against the same FHIR base the connection was
        # minted against, not the configured default.
        token = vendor_mod.refresh(refresh_token, iss_override=conn.iss)
        new_access_enc = encrypt_token(token.access_token)
        new_refresh_enc = encrypt_token(token.refresh_token) if token.refresh_token else None
        new_expires_at = now + timedelta(seconds=token.expires_in)
        storage.update_ehr_connection_tokens(
            conn.id,
            access_token_enc=new_access_enc,
            refresh_token_enc=new_refresh_enc,
            expires_at=new_expires_at,
        )
        logger.info("Refreshed EHR token for connection_id=%d", conn.id)
        audit.record(
            storage,
            action="ehr.token_refreshed",
            request=None,
            actor_user_id=conn.user_id,
            metadata={"ehr_vendor": vendor},
        )
        return str(token.access_token)
    except Exception:
        logger.exception("EHR token refresh failed for connection_id=%d", conn.id)
        audit.record(
            storage,
            action="ehr.token_refresh_failed",
            request=None,
            actor_user_id=conn.user_id,
            metadata={"ehr_vendor": vendor},
        )
        return access_token
