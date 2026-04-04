"""Authentication utilities for docstats."""

from __future__ import annotations

from fastapi import Depends, Request
from passlib.context import CryptContext

from docstats.storage import Storage, get_storage

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

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
    """Return the logged-in user dict, or None if not authenticated."""
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    return storage.get_user_by_id(user_id)


def require_user(user: dict | None = Depends(get_current_user)) -> dict:
    """FastAPI dependency that raises AuthRequiredException if not logged in."""
    if user is None:
        raise AuthRequiredException()
    return user


def get_anon_search_count(request: Request) -> int:
    return request.session.get("anon_searches", 0)


def increment_anon_search_count(request: Request) -> int:
    count = request.session.get("anon_searches", 0) + 1
    request.session["anon_searches"] = count
    return count
