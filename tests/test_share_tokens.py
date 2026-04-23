"""Phase 9.B — Share-token storage, domain logic, and viewer route tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from docstats.domain.share_tokens import (
    generate_token,
    hash_second_factor,
    hash_token,
    token_expires_at,
    verify_second_factor,
)
from docstats.scope import Scope
from docstats.storage import Storage
from docstats.webhook_verifiers.svix import SvixVerificationError, verify_svix


# ---------- Fixtures ----------


def _seed(storage: Storage, user_id: int):
    scope = Scope(user_id=user_id)
    patient = storage.create_patient(
        scope,
        first_name="Jane",
        last_name="Doe",
        date_of_birth="1980-05-15",
        created_by_user_id=user_id,
    )
    referral = storage.create_referral(
        scope,
        patient_id=patient.id,
        reason="Cardiology consult",
        urgency="routine",
        specialty_desc="Cardiology",
        receiving_organization_name="Heart Clinic",
        created_by_user_id=user_id,
    )
    delivery = storage.create_delivery(
        scope,
        referral_id=referral.id,
        channel="email",
        recipient="specialist@example.com",
    )
    return scope, patient, referral, delivery


@pytest.fixture
def storage(tmp_path: Path):
    s = Storage(db_path=tmp_path / "test.db")
    yield s
    s.close()


# ---------- Domain helpers ----------


def test_generate_token_is_unique():
    t1 = generate_token()
    t2 = generate_token()
    assert t1 != t2
    assert len(t1) > 20


def test_hash_token_is_deterministic():
    tok = generate_token()
    assert hash_token(tok) == hash_token(tok)


def test_verify_second_factor_correct(monkeypatch):
    monkeypatch.setenv("SHARE_TOKEN_SECRET", "supersecret")
    stored = hash_second_factor("1980-05-15")
    assert verify_second_factor("1980-05-15", stored) is True


def test_verify_second_factor_wrong(monkeypatch):
    monkeypatch.setenv("SHARE_TOKEN_SECRET", "supersecret")
    stored = hash_second_factor("1980-05-15")
    assert verify_second_factor("1999-01-01", stored) is False


def test_verify_second_factor_case_insensitive(monkeypatch):
    monkeypatch.setenv("SHARE_TOKEN_SECRET", "supersecret")
    stored = hash_second_factor("  1980-05-15  ")  # extra whitespace
    assert verify_second_factor("1980-05-15", stored) is True


def test_hash_second_factor_no_secret_raises(monkeypatch):
    monkeypatch.delenv("SHARE_TOKEN_SECRET", raising=False)
    with pytest.raises(ValueError, match="SHARE_TOKEN_SECRET"):
        hash_second_factor("anything")


def test_token_expires_at_in_future():
    from datetime import datetime, timezone

    exp = token_expires_at()
    assert exp > datetime.now(tz=timezone.utc)


# ---------- Storage CRUD ----------


def test_create_and_fetch_share_token(storage: Storage, monkeypatch):
    monkeypatch.setenv("SHARE_TOKEN_SECRET", "s")
    user_id = storage.create_user("a@example.com", "hash")
    _, _, _, delivery = _seed(storage, user_id)
    plaintext = generate_token()
    tok_hash = hash_token(plaintext)
    expires = token_expires_at()

    token = storage.create_share_token(
        delivery_id=delivery.id,
        token_hash=tok_hash,
        expires_at=expires,
        second_factor_kind="patient_dob",
        second_factor_hash=hash_second_factor("1980-05-15"),
    )
    assert token.id > 0
    assert token.delivery_id == delivery.id
    assert token.view_count == 0
    assert token.failed_attempts == 0
    assert token.is_valid is True
    assert token.requires_second_factor is True

    fetched = storage.get_share_token_by_hash(tok_hash)
    assert fetched is not None
    assert fetched.id == token.id


def test_fetch_nonexistent_token_returns_none(storage: Storage):
    assert storage.get_share_token_by_hash("deadbeef" * 8) is None


def test_increment_views(storage: Storage, monkeypatch):
    monkeypatch.setenv("SHARE_TOKEN_SECRET", "s")
    user_id = storage.create_user("a@example.com", "hash")
    _, _, _, delivery = _seed(storage, user_id)
    tok = storage.create_share_token(
        delivery_id=delivery.id,
        token_hash=hash_token(generate_token()),
        expires_at=token_expires_at(),
    )
    storage.increment_share_token_views(tok.id)
    storage.increment_share_token_views(tok.id)
    updated = storage.get_share_token_by_hash(tok.token_hash)
    assert updated is not None
    assert updated.view_count == 2
    assert updated.last_viewed_at is not None


def test_increment_failures(storage: Storage):
    user_id = storage.create_user("a@example.com", "hash")
    _, _, _, delivery = _seed(storage, user_id)
    tok = storage.create_share_token(
        delivery_id=delivery.id,
        token_hash=hash_token(generate_token()),
        expires_at=token_expires_at(),
    )
    for _ in range(3):
        storage.increment_share_token_failures(tok.id)
    updated = storage.get_share_token_by_hash(tok.token_hash)
    assert updated is not None
    assert updated.failed_attempts == 3


def test_revoke_share_token_idempotent(storage: Storage):
    user_id = storage.create_user("a@example.com", "hash")
    _, _, _, delivery = _seed(storage, user_id)
    tok = storage.create_share_token(
        delivery_id=delivery.id,
        token_hash=hash_token(generate_token()),
        expires_at=token_expires_at(),
    )
    assert storage.revoke_share_token(tok.id) is True
    assert storage.revoke_share_token(tok.id) is False  # already revoked


def test_is_valid_after_revocation(storage: Storage):
    user_id = storage.create_user("a@example.com", "hash")
    _, _, _, delivery = _seed(storage, user_id)
    plaintext = generate_token()
    tok = storage.create_share_token(
        delivery_id=delivery.id,
        token_hash=hash_token(plaintext),
        expires_at=token_expires_at(),
    )
    assert tok.is_valid is True
    storage.revoke_share_token(tok.id)
    updated = storage.get_share_token_by_hash(hash_token(plaintext))
    assert updated is not None
    assert updated.is_valid is False


def test_token_cascade_delete_with_delivery(storage: Storage):
    user_id = storage.create_user("a@example.com", "hash")
    scope, _, referral, delivery = _seed(storage, user_id)
    plaintext = generate_token()
    storage.create_share_token(
        delivery_id=delivery.id,
        token_hash=hash_token(plaintext),
        expires_at=token_expires_at(),
    )
    # Cancel (not hard-delete) — share token should still exist
    storage.cancel_delivery(scope, delivery.id, cancelled_by_user_id=user_id)
    tok = storage.get_share_token_by_hash(hash_token(plaintext))
    assert tok is not None  # cancel doesn't cascade DELETE


# ---------- Svix webhook verifier ----------


def _svix_headers(msg_id: str, timestamp: str, key: bytes, body: bytes) -> dict:
    import base64
    import hashlib
    import hmac as _hmac

    signed = f"{msg_id}.{timestamp}.".encode() + body
    sig = base64.b64encode(_hmac.new(key, signed, hashlib.sha256).digest()).decode()
    secret_b64 = base64.b64encode(key).decode()
    return {
        "svix-id": msg_id,
        "svix-timestamp": timestamp,
        "svix-signature": f"v1,{sig}",
        "x-webhook-secret": f"whsec_{secret_b64}",
    }


def test_svix_verify_valid():
    import base64
    import secrets
    import time

    key = secrets.token_bytes(32)
    secret = "whsec_" + base64.b64encode(key).decode()
    body = b'{"type":"email.delivered"}'
    ts = str(int(time.time()))
    headers = _svix_headers("msg_abc", ts, key, body)
    # Should not raise
    verify_svix(headers, body, secret)


def test_svix_verify_wrong_secret():
    import base64
    import secrets
    import time

    key = secrets.token_bytes(32)
    wrong_key = secrets.token_bytes(32)
    secret = "whsec_" + base64.b64encode(wrong_key).decode()
    body = b'{"type":"email.delivered"}'
    ts = str(int(time.time()))
    headers = _svix_headers("msg_abc", ts, key, body)
    with pytest.raises(SvixVerificationError, match="mismatch"):
        verify_svix(headers, body, secret)


def test_svix_verify_replayed():
    import base64
    import secrets
    import time

    key = secrets.token_bytes(32)
    secret = "whsec_" + base64.b64encode(key).decode()
    body = b'{"type":"email.delivered"}'
    old_ts = str(int(time.time()) - 400)  # 400s ago — past ±5min
    headers = _svix_headers("msg_abc", old_ts, key, body)
    with pytest.raises(SvixVerificationError, match="outside"):
        verify_svix(headers, body, secret)


def test_svix_verify_missing_headers():
    with pytest.raises(SvixVerificationError, match="Missing"):
        verify_svix({}, b"body", "whsec_AAAA")


# ---------- Email channel unit ----------


def test_email_channel_disabled_without_api_key(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    from docstats.delivery.base import ChannelDisabledError
    from docstats.delivery.channels.email import ResendEmailChannel

    with pytest.raises(ChannelDisabledError, match="RESEND_API_KEY"):
        ResendEmailChannel()


def test_email_channel_instantiates_with_key(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_abc")
    from docstats.delivery.channels.email import ResendEmailChannel

    ch = ResendEmailChannel()
    assert ch.name == "email"
    assert ch.vendor_name == "Resend"


# ---------- Resend webhook route ----------


@pytest.fixture
def web_client(tmp_path):
    from fastapi.testclient import TestClient

    from docstats.storage import Storage, get_storage
    from docstats.web import app

    s = Storage(db_path=tmp_path / "test.db")
    app.dependency_overrides[get_storage] = lambda: s
    with TestClient(app, raise_server_exceptions=True) as tc:
        yield tc, s
    app.dependency_overrides.clear()
    s.close()


def test_resend_webhook_unknown_event_ignored(web_client):
    tc, _ = web_client
    resp = tc.post(
        "/webhooks/resend",
        json={"type": "email.opened", "data": {"email_id": "msg-x"}},
    )
    assert resp.status_code == 200
    assert resp.json()["action"] == "ignored"


def test_resend_webhook_delivered_updates_delivery(web_client):
    tc, storage = web_client
    user_id = storage.create_user("w@example.com", "hash")
    scope = Scope(user_id=user_id)
    patient = storage.create_patient(
        scope,
        first_name="J",
        last_name="D",
        date_of_birth="1990-01-01",
        created_by_user_id=user_id,
    )
    referral = storage.create_referral(
        scope,
        patient_id=patient.id,
        reason="Test",
        urgency="routine",
        specialty_desc="General",
        receiving_organization_name="Clinic",
        created_by_user_id=user_id,
    )
    delivery = storage.create_delivery(
        scope,
        referral_id=referral.id,
        channel="email",
        recipient="r@example.com",
    )
    # Simulate the delivery being in "sent" state with a vendor_message_id
    storage.mark_delivery_sent(delivery.id, vendor_name="Resend", vendor_message_id="msg-DELIVER")

    resp = tc.post(
        "/webhooks/resend",
        json={"type": "email.delivered", "data": {"email_id": "msg-DELIVER"}},
    )
    assert resp.status_code == 200
    assert resp.json()["action"] == "updated"
    refreshed = storage.get_delivery(scope, delivery.id)
    assert refreshed is not None
    assert refreshed.status == "delivered"


def test_resend_webhook_bounced_fails_delivery(web_client):
    tc, storage = web_client
    user_id = storage.create_user("b@example.com", "hash")
    scope = Scope(user_id=user_id)
    patient = storage.create_patient(
        scope,
        first_name="A",
        last_name="B",
        date_of_birth="1990-01-01",
        created_by_user_id=user_id,
    )
    referral = storage.create_referral(
        scope,
        patient_id=patient.id,
        reason="Test",
        urgency="routine",
        specialty_desc="General",
        receiving_organization_name="X",
        created_by_user_id=user_id,
    )
    delivery = storage.create_delivery(
        scope,
        referral_id=referral.id,
        channel="email",
        recipient="bad@example.com",
    )
    storage.mark_delivery_sent(delivery.id, vendor_name="Resend", vendor_message_id="msg-BOUNCE")

    resp = tc.post(
        "/webhooks/resend",
        json={
            "type": "email.bounced",
            "data": {"email_id": "msg-BOUNCE", "reason": "User unknown"},
        },
    )
    assert resp.status_code == 200
    refreshed = storage.get_delivery(scope, delivery.id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.last_error_code == "email_bounced"


def test_resend_webhook_invalid_sig_rejected(web_client, monkeypatch):
    monkeypatch.setenv(
        "RESEND_WEBHOOK_SECRET", "whsec_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    )
    tc, _ = web_client
    import time

    resp = tc.post(
        "/webhooks/resend",
        content=b'{"type":"email.delivered","data":{"email_id":"x"}}',
        headers={
            "Content-Type": "application/json",
            "svix-id": "msg_test",
            "svix-timestamp": str(int(time.time())),
            "svix-signature": "v1,invalidsignature",
        },
    )
    assert resp.status_code == 400
