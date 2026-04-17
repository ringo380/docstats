"""Tests for server-side sessions (Phase 0.C)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from docstats.auth import get_current_user
from docstats.domain.sessions import Session
from docstats.storage import Storage


@pytest.fixture
def user_id(storage: Storage) -> int:
    return storage.create_user("sessions@example.com", "hashed")


# --- Session model ---


def test_session_is_active_when_fresh() -> None:
    now = datetime.now(tz=timezone.utc)
    session = Session(
        id="abc",
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=1),
    )
    assert session.is_active() is True


def test_session_is_inactive_when_revoked() -> None:
    now = datetime.now(tz=timezone.utc)
    session = Session(
        id="abc",
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=1),
        revoked_at=now,
    )
    assert session.is_active() is False


def test_session_is_inactive_when_expired() -> None:
    now = datetime.now(tz=timezone.utc)
    session = Session(
        id="abc",
        created_at=now - timedelta(days=10),
        last_seen_at=now - timedelta(days=10),
        expires_at=now - timedelta(hours=1),
    )
    assert session.is_active() is False


def test_session_is_active_accepts_injected_now() -> None:
    """Naive-datetime ``now`` must be treated as UTC for consistent comparisons."""
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session = Session(
        id="abc",
        created_at=ts,
        last_seen_at=ts,
        expires_at=ts + timedelta(hours=1),
    )
    assert session.is_active(now=ts + timedelta(minutes=30)) is True
    assert session.is_active(now=ts + timedelta(hours=2)) is False
    assert session.is_active(now=datetime(2026, 1, 1, 0, 30)) is True  # naive


# --- Storage CRUD ---


def test_create_session_assigns_opaque_id(storage: Storage, user_id: int) -> None:
    session = storage.create_session(user_id=user_id, ip="203.0.113.7", user_agent="pytest")
    assert len(session.id) >= 32  # secrets.token_urlsafe(32) gives ~43 chars
    assert session.user_id == user_id
    assert session.ip == "203.0.113.7"
    assert session.user_agent == "pytest"
    assert session.revoked_at is None
    assert session.is_active() is True


def test_create_session_anonymous(storage: Storage) -> None:
    """Anonymous sessions (user_id=None) must be allowed — used on cookie issue."""
    session = storage.create_session(ttl_seconds=3600)
    assert session.user_id is None
    assert session.is_active() is True


def test_get_session_returns_row(storage: Storage, user_id: int) -> None:
    created = storage.create_session(user_id=user_id)
    fetched = storage.get_session(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.user_id == user_id


def test_get_session_returns_none_for_unknown_id(storage: Storage) -> None:
    assert storage.get_session("not-a-real-id") is None


def test_get_session_returns_revoked_row(storage: Storage, user_id: int) -> None:
    """get_session returns the row regardless of state; callers check is_active."""
    created = storage.create_session(user_id=user_id)
    storage.revoke_session(created.id)
    fetched = storage.get_session(created.id)
    assert fetched is not None
    assert fetched.revoked_at is not None
    assert fetched.is_active() is False


def test_revoke_session(storage: Storage, user_id: int) -> None:
    session = storage.create_session(user_id=user_id)
    assert storage.revoke_session(session.id) is True
    # Second revoke is a no-op.
    assert storage.revoke_session(session.id) is False

    refetched = storage.get_session(session.id)
    assert refetched is not None
    assert refetched.is_active() is False


def test_touch_session_updates_last_seen(storage: Storage, user_id: int) -> None:
    session = storage.create_session(user_id=user_id)
    original_seen = session.last_seen_at
    time.sleep(0.01)
    assert storage.touch_session(session.id) is True
    refetched = storage.get_session(session.id)
    assert refetched is not None
    assert refetched.last_seen_at >= original_seen


def test_touch_session_does_not_revive_revoked(storage: Storage, user_id: int) -> None:
    session = storage.create_session(user_id=user_id)
    storage.revoke_session(session.id)
    assert storage.touch_session(session.id) is False


def test_touch_session_optionally_updates_ip_and_ua(storage: Storage, user_id: int) -> None:
    session = storage.create_session(user_id=user_id, ip="1.1.1.1", user_agent="old")
    storage.touch_session(session.id, ip="2.2.2.2", user_agent="new")
    refetched = storage.get_session(session.id)
    assert refetched is not None
    assert refetched.ip == "2.2.2.2"
    assert refetched.user_agent == "new"


def test_list_sessions_for_user_excludes_revoked(storage: Storage, user_id: int) -> None:
    a = storage.create_session(user_id=user_id)
    b = storage.create_session(user_id=user_id)
    storage.revoke_session(b.id)

    sessions = storage.list_sessions_for_user(user_id)
    ids = {s.id for s in sessions}
    assert a.id in ids
    assert b.id not in ids


def test_purge_expired_sessions(storage: Storage, user_id: int) -> None:
    # A short-TTL session (already expired after we push clock forward logically)
    short = storage.create_session(user_id=user_id, ttl_seconds=0)
    long = storage.create_session(user_id=user_id, ttl_seconds=86400)

    deleted = storage.purge_expired_sessions()
    # Only the 0-TTL row expired.
    assert deleted >= 1
    assert storage.get_session(short.id) is None
    assert storage.get_session(long.id) is not None


def test_user_delete_cascades_sessions(storage: Storage) -> None:
    """Sessions are derived from users — CASCADE on user deletion is correct."""
    uid = storage.create_user("doomed@example.com", "hashed")
    session = storage.create_session(user_id=uid)
    storage.delete_provider  # noqa: B018 — sanity touch; storage API is in place
    storage._conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    storage._conn.commit()
    assert storage.get_session(session.id) is None


# --- get_current_user session validation ---


def _fake_request(session_dict: dict) -> MagicMock:
    req = MagicMock()
    req.session = session_dict
    return req


def test_get_current_user_returns_none_without_session(storage: Storage) -> None:
    user = get_current_user(_fake_request({}), storage=storage)
    assert user is None


def test_get_current_user_grandfathers_legacy_cookie(storage: Storage, user_id: int) -> None:
    """A cookie with ``user_id`` but no ``session_id`` predates Phase 0.C —
    we still accept it (legacy cookies shouldn't force every user to re-login).
    Next login upgrades the cookie to a proper session row."""
    user = get_current_user(_fake_request({"user_id": user_id}), storage=storage)
    assert user is not None
    assert user["id"] == user_id


def test_get_current_user_accepts_active_session(storage: Storage, user_id: int) -> None:
    session = storage.create_session(user_id=user_id)
    req_session = {"user_id": user_id, "session_id": session.id}
    user = get_current_user(_fake_request(req_session), storage=storage)
    assert user is not None
    assert user["id"] == user_id


def test_get_current_user_rejects_revoked_session(storage: Storage, user_id: int) -> None:
    session = storage.create_session(user_id=user_id)
    storage.revoke_session(session.id)
    req_session = {"user_id": user_id, "session_id": session.id}
    user = get_current_user(_fake_request(req_session), storage=storage)
    assert user is None
    # Cookie was cleared so the next request short-circuits.
    assert req_session == {}


def test_get_current_user_rejects_missing_session_row(storage: Storage, user_id: int) -> None:
    """If the cookie references a session_id that doesn't exist in DB (DB wiped,
    app reinstalled, token fabricated), treat it as unauthenticated."""
    req_session = {"user_id": user_id, "session_id": "not-a-real-id"}
    user = get_current_user(_fake_request(req_session), storage=storage)
    assert user is None
    assert req_session == {}


def test_get_current_user_tolerates_storage_failure(user_id: int) -> None:
    """DB outage on session lookup must not 500 the request. We deny access
    (return None) rather than grant it — fail-closed on auth."""
    broken = MagicMock()
    broken.get_session.side_effect = RuntimeError("db offline")
    req_session = {"user_id": user_id, "session_id": "some-id"}
    user = get_current_user(_fake_request(req_session), storage=broken)
    assert user is None


# --- touch_session wiring in get_current_user ---


def test_get_current_user_touches_stale_session(storage: Storage, user_id: int) -> None:
    """If last_seen_at is older than the touch grace window, get_current_user
    refreshes it. Otherwise the session row's last_seen_at never advances from
    created_at, and 'active session' UX can't tell if a user is still active."""
    session = storage.create_session(user_id=user_id)
    # Force last_seen_at into the past so the touch-grace check fires.
    storage._conn.execute(
        "UPDATE sessions SET last_seen_at = datetime('now', '-10 minutes') WHERE id = ?",
        (session.id,),
    )
    storage._conn.commit()
    before = storage.get_session(session.id)
    assert before is not None
    req_session = {"user_id": user_id, "session_id": session.id}
    get_current_user(_fake_request(req_session), storage=storage)
    after = storage.get_session(session.id)
    assert after is not None
    assert after.last_seen_at > before.last_seen_at


def test_get_current_user_skips_touch_within_grace(storage: Storage, user_id: int) -> None:
    """Hot-path reads shouldn't cost a DB write on every request."""
    session = storage.create_session(user_id=user_id)
    req_session = {"user_id": user_id, "session_id": session.id}

    wrapper = MagicMock(wraps=storage)
    get_current_user(_fake_request(req_session), storage=wrapper)
    # Fresh session (last_seen_at == created_at, both "now") is inside the
    # 5-minute grace window, so touch_session must not be called.
    wrapper.touch_session.assert_not_called()


def test_get_current_user_tolerates_touch_failure(storage: Storage, user_id: int) -> None:
    """A failed touch must never fail the auth check."""
    session = storage.create_session(user_id=user_id)
    storage._conn.execute(
        "UPDATE sessions SET last_seen_at = datetime('now', '-10 minutes') WHERE id = ?",
        (session.id,),
    )
    storage._conn.commit()

    class FlakyStorage:
        def __init__(self, inner: Storage) -> None:
            self._inner = inner

        def get_session(self, sid: str):
            return self._inner.get_session(sid)

        def get_user_by_id(self, uid: int):
            return self._inner.get_user_by_id(uid)

        def touch_session(self, *args, **kwargs):
            raise RuntimeError("db hiccup")

    req_session = {"user_id": user_id, "session_id": session.id}
    user = get_current_user(_fake_request(req_session), storage=FlakyStorage(storage))  # type: ignore[arg-type]
    assert user is not None
    assert user["id"] == user_id
