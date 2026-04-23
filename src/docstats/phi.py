"""PHI consent primitives.

Consent for PHI entry is tracked separately from the general Terms of Service
(``users.terms_accepted_at``). A PHI version bump does not force re-acceptance
of the general ToS, and vice versa.

Phase 0.D ships the storage columns + this module. The first PHI-entry route in
Phase 2 will call :func:`require_phi_consent` as a dependency; routes that only
touch public NPPES data (lookup, save-to-rolodex, search history) stay gated
only by :func:`docstats.auth.require_user`.

``CURRENT_PHI_CONSENT_VERSION`` bumps trigger re-acceptance on next PHI-route
hit (not on login). Keep it a simple string like ``"1.0"``.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException

from docstats.auth import AuthRequiredException, require_user, require_user_api

CURRENT_PHI_CONSENT_VERSION = "1.0"


class PhiConsentRequiredException(AuthRequiredException):
    """Raised when a user attempts a PHI-entry route without a current consent.

    Extends ``AuthRequiredException`` so the existing exception handler in
    ``web.py`` still catches it; Phase 2 may add a dedicated handler that
    redirects to a dedicated PHI-consent prompt instead of the login page.
    """


def _has_current_phi_consent(user: dict) -> bool:
    return (
        user.get("phi_consent_at") is not None
        and user.get("phi_consent_version") == CURRENT_PHI_CONSENT_VERSION
    )


def require_phi_consent(user: dict = Depends(require_user)) -> dict:
    """FastAPI dependency: the user is logged in *and* has current PHI consent.

    Browser variant — re-raises ``PhiConsentRequiredException`` (a subclass
    of ``AuthRequiredException``) which the global handler converts to a
    303 redirect to ``/auth/login``. For API endpoints use
    :func:`require_phi_consent_api` instead so consumers get JSON errors.
    """
    if not _has_current_phi_consent(user):
        raise PhiConsentRequiredException()
    return user


def require_phi_consent_api(user: dict = Depends(require_user_api)) -> dict:
    """FastAPI dependency for API endpoints that need PHI consent.

    Unauthenticated callers hit ``require_user_api`` and get a 401 JSON
    response. Authenticated-but-not-consented callers get a 403 JSON
    response — API consumers can't complete the consent flow over the
    wire in Phase 8, so this is terminal (they must go through the web UI
    to consent once, then their cookie works for subsequent API calls).
    """
    if not _has_current_phi_consent(user):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "phi_consent_required",
                "message": "PHI consent is required for this endpoint. Complete consent via the web UI.",
            },
        )
    return user
