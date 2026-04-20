"""Authentication utilities for docstats."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Request
from passlib.context import CryptContext

from docstats.domain.orgs import has_role_at_least
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__ident="2b")

ANON_SEARCH_LIMIT = 3

# Skip the session touch unless last_seen_at is older than this — prevents one
# DB write per request on hot paths. Chosen so "last active X ago" UI is still
# accurate to the minute without burning round-trips on tight page-navigation.
_TOUCH_MIN_INTERVAL = timedelta(minutes=5)


class AuthRequiredException(Exception):
    """Raised when a route requires authentication but the user is not logged in."""


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def get_current_user(
    request: Request,
    storage: StorageBase = Depends(get_storage),
) -> dict | None:
    """Return the logged-in user dict, or None if not authenticated.

    Validates that the session row is still active (not revoked, not expired).
    A revoked session clears the cookie and returns None so the next request
    treats the caller as anonymous. Legacy cookies (from before Phase 0.C)
    that carry ``user_id`` but no ``session_id`` are grandfathered in for one
    request cycle. They upgrade to a proper session row the next time the
    user successfully logs in with the DB reachable — if the DB is down on
    subsequent login attempts, the cookie stays in legacy mode until the DB
    recovers and a login succeeds.

    DB lookup failures on ``get_session`` fail CLOSED: we deny access rather
    than grant it, so a transient outage can't be exploited to bypass
    revocation.

    Side effect: if ``last_seen_at`` on the session row is older than
    ``_TOUCH_MIN_INTERVAL``, updates it. This keeps activity tracking
    reasonably fresh without a write per request.
    """
    user_id = request.session.get("user_id")
    if user_id is None:
        return None

    session_id = request.session.get("session_id")
    if session_id is not None:
        try:
            session = storage.get_session(session_id)
        except Exception:
            logger.exception("Session lookup failed; denying request")
            return None

        if session is None or not session.is_active():
            # Revoked, expired, or row vanished. Clear the cookie so subsequent
            # requests don't hit the DB on a dead token.
            request.session.clear()
            return None

        # Update last_seen_at if it's stale. Best-effort; don't fail the
        # request if the touch write fails.
        now = datetime.now(tz=timezone.utc)
        last_seen = session.last_seen_at
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if now - last_seen >= _TOUCH_MIN_INTERVAL:
            try:
                storage.touch_session(session_id)
            except Exception:
                logger.exception("Session touch failed for %s", session_id)

    user = storage.get_user_by_id(user_id)
    if user is None:
        return None

    # Enrich with active-org admin flag so templates (notably base.html nav)
    # can show/hide the Admin link without every route computing it. Costs
    # one membership lookup per authenticated request when active_org_id is
    # set; no cost for solo users. Lookup failures degrade to False (user
    # won't see the Admin link) — same fail-closed posture as get_session.
    user["is_org_admin"] = False
    active_org_id = user.get("active_org_id")
    if active_org_id is not None:
        try:
            membership = storage.get_membership(active_org_id, user_id)
            if membership is not None and membership.is_active:
                user["is_org_admin"] = has_role_at_least(membership.role, "admin")
        except Exception:
            logger.exception("Admin-flag membership lookup failed user_id=%s", user_id)

    return user


def require_user(user: dict | None = Depends(get_current_user)) -> dict:
    """FastAPI dependency that raises AuthRequiredException if not logged in."""
    if user is None:
        raise AuthRequiredException()
    return user


def get_anon_search_count(request: Request) -> int:
    return int(request.session.get("anon_searches", 0))


def increment_anon_search_count(request: Request) -> int:
    count = int(request.session.get("anon_searches", 0)) + 1
    request.session["anon_searches"] = count
    return count
