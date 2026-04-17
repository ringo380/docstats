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

from fastapi import Depends

from docstats.auth import AuthRequiredException, require_user

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

    No route uses this yet. Phase 2 PHI-entry routes (patient create, referral
    clinical fields, attachments) will depend on it.
    """
    if not _has_current_phi_consent(user):
        raise PhiConsentRequiredException()
    return user
