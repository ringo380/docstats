"""Authentication utilities for docstats."""

from __future__ import annotations

import logging

from fastapi import Depends, Request
from passlib.context import CryptContext

from docstats.storage import Storage, get_storage

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__ident="2b")

ANON_SEARCH_LIMIT = 3


class AuthRequiredException(Exception):
    """Raised when a route requires authentication but the user is not logged in."""


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def get_current_user(
    request: Request,
    storage: Storage = Depends(get_storage),
) -> dict | None:
    """Return the logged-in user dict, or None if not authenticated.

    Validates that the session row is still active (not revoked, not expired).
    A revoked session clears the cookie and returns None so the next request
    treats the caller as anonymous. Legacy cookies (from before Phase 0.C) that
    carry ``user_id`` but no ``session_id`` are grandfathered in for one
    request cycle; the next login upgrades them to a proper session row.
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

    return storage.get_user_by_id(user_id)


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
