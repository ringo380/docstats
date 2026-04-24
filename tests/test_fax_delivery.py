"""Phase 9.C — Documo fax channel + webhook route + validator tests."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from docstats.delivery.base import (
    ChannelDisabledError,
    DeliveryError,
)
from docstats.scope import Scope
from docstats.storage import Storage
from docstats.validators import ValidationError, validate_fax_number
from docstats.webhook_verifiers.documo import DocumoVerificationError, verify_documo


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
        channel="fax",
        recipient="+15555551234",
    )
    return scope, patient, referral, delivery


@pytest.fixture
def storage(tmp_path: Path):
    s = Storage(db_path=tmp_path / "test.db")
    yield s
    s.close()


# ---------- Fax number validator ----------


def test_validate_fax_number_accepts_bare_10_digit():
    assert validate_fax_number("5555551234") == "+15555551234"


def test_validate_fax_number_accepts_formatted():
    assert validate_fax_number("(555) 555-1234") == "+15555551234"
    assert validate_fax_number("555.555.1234") == "+15555551234"
    assert validate_fax_number("+1 555 555 1234") == "+15555551234"


def test_validate_fax_number_accepts_11_digit_with_country():
    assert validate_fax_number("15555551234") == "+15555551234"


def test_validate_fax_number_rejects_too_short():
    with pytest.raises(ValidationError):
        validate_fax_number("555")


def test_validate_fax_number_rejects_non_us():
    # UK, DE, etc — 11+ digits but not starting with 1
    with pytest.raises(ValidationError):
        validate_fax_number("+442071838750")


def test_validate_fax_number_rejects_empty():
    with pytest.raises(ValidationError):
        validate_fax_number("")
    with pytest.raises(ValidationError):
        validate_fax_number("   ")


def test_validate_fax_number_rejects_overlong():
    with pytest.raises(ValidationError):
        validate_fax_number("+1 555 555 1234 x12345678")


# ---------- Channel factory ----------


def test_fax_channel_disabled_without_api_key(monkeypatch):
    monkeypatch.delenv("DOCUMO_API_KEY", raising=False)
    from docstats.delivery.channels.fax import DocumoFaxChannel

    with pytest.raises(ChannelDisabledError, match="DOCUMO_API_KEY"):
        DocumoFaxChannel()


def test_fax_channel_instantiates_with_key(monkeypatch):
    monkeypatch.setenv("DOCUMO_API_KEY", "doc_test_abc")
    from docstats.delivery.channels.fax import DocumoFaxChannel

    ch = DocumoFaxChannel()
    assert ch.name == "fax"
    assert ch.vendor_name == "Documo"


def test_fax_channel_enabled_in_registry_when_key_set(monkeypatch):
    monkeypatch.setenv("DOCUMO_API_KEY", "doc_test_abc")
    from docstats.delivery.registry import enabled_channels

    assert "fax" in enabled_channels()


def test_fax_channel_absent_from_registry_when_key_missing(monkeypatch):
    monkeypatch.delenv("DOCUMO_API_KEY", raising=False)
    from docstats.delivery.registry import enabled_channels

    assert "fax" not in enabled_channels()


# ---------- Channel.send() happy path + error paths ----------


class _FakeDelivery:
    """Minimal shape of ``Delivery`` that the fax channel touches."""

    def __init__(
        self,
        *,
        id: int = 1,
        referral_id: int = 1,
        recipient: str = "+15555551234",
        idempotency_key: str | None = "fax:abc",
        packet_artifact: dict | None = None,
    ) -> None:
        self.id = id
        self.referral_id = referral_id
        self.recipient = recipient
        self.idempotency_key = idempotency_key
        self.packet_artifact = packet_artifact or {}


@pytest.mark.asyncio
async def test_fax_send_success(monkeypatch):
    monkeypatch.setenv("DOCUMO_API_KEY", "doc_test_abc")
    from docstats.delivery.channels.fax import DocumoFaxChannel

    ch = DocumoFaxChannel()
    delivery = _FakeDelivery(
        packet_artifact={"fax_subject": "Referral: Cardiology", "recipient_name": "Heart Clinic"}
    )

    mock_response = httpx.Response(
        201,
        json={"id": "fx_123", "status": "queued"},
        request=httpx.Request("POST", "https://api.documo.com/v1/fax/send"),
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_response)) as post:
        receipt = await ch.send(delivery, b"%PDF-1.4\n<fake>")

    assert receipt.vendor_name == "Documo"
    assert receipt.vendor_message_id == "fx_123"
    assert receipt.status == "sent"
    # Header assertions
    call = post.call_args
    headers = call.kwargs["headers"]
    assert headers["Authorization"] == "Basic doc_test_abc"
    assert headers["Idempotency-Key"] == "fax:abc"
    # Body assertions — multipart fields
    data = call.kwargs["data"]
    assert data["recipientFax"] == "+15555551234"
    assert data["subject"] == "Referral: Cardiology"
    assert data["recipientName"] == "Heart Clinic"
    files = call.kwargs["files"]
    assert files["files"][2] == "application/pdf"


@pytest.mark.asyncio
async def test_fax_send_accepts_legacy_fax_id_field(monkeypatch):
    """Some Documo accounts return ``faxId`` instead of ``id``."""
    monkeypatch.setenv("DOCUMO_API_KEY", "doc_test_abc")
    from docstats.delivery.channels.fax import DocumoFaxChannel

    ch = DocumoFaxChannel()
    delivery = _FakeDelivery()

    mock_response = httpx.Response(
        200,
        json={"faxId": "legacy_id_42"},
        request=httpx.Request("POST", "https://api.documo.com/v1/fax/send"),
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_response)):
        receipt = await ch.send(delivery, b"pdf")

    assert receipt.vendor_message_id == "legacy_id_42"


@pytest.mark.asyncio
async def test_fax_send_empty_packet_raises_fatal(monkeypatch):
    monkeypatch.setenv("DOCUMO_API_KEY", "doc_test_abc")
    from docstats.delivery.channels.fax import DocumoFaxChannel

    ch = DocumoFaxChannel()
    delivery = _FakeDelivery()

    with pytest.raises(DeliveryError) as exc:
        await ch.send(delivery, b"")
    assert exc.value.error_code == "empty_packet"
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_fax_send_rate_limit_is_retryable(monkeypatch):
    monkeypatch.setenv("DOCUMO_API_KEY", "doc_test_abc")
    from docstats.delivery.channels.fax import DocumoFaxChannel

    ch = DocumoFaxChannel()
    delivery = _FakeDelivery()

    mock_response = httpx.Response(
        429,
        text="rate limited",
        request=httpx.Request("POST", "https://api.documo.com/v1/fax/send"),
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(DeliveryError) as exc:
            await ch.send(delivery, b"pdf")
    assert exc.value.error_code == "rate_limited"
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_fax_send_5xx_is_retryable(monkeypatch):
    monkeypatch.setenv("DOCUMO_API_KEY", "doc_test_abc")
    from docstats.delivery.channels.fax import DocumoFaxChannel

    ch = DocumoFaxChannel()
    delivery = _FakeDelivery()

    mock_response = httpx.Response(
        503,
        text="gateway down",
        request=httpx.Request("POST", "https://api.documo.com/v1/fax/send"),
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(DeliveryError) as exc:
            await ch.send(delivery, b"pdf")
    assert exc.value.error_code == "vendor_5xx"
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_fax_send_4xx_is_fatal(monkeypatch):
    monkeypatch.setenv("DOCUMO_API_KEY", "doc_test_abc")
    from docstats.delivery.channels.fax import DocumoFaxChannel

    ch = DocumoFaxChannel()
    delivery = _FakeDelivery(recipient="invalid")

    mock_response = httpx.Response(
        400,
        text="bad recipient",
        request=httpx.Request("POST", "https://api.documo.com/v1/fax/send"),
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(DeliveryError) as exc:
            await ch.send(delivery, b"pdf")
    assert exc.value.error_code == "vendor_4xx"
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_fax_send_timeout_is_retryable(monkeypatch):
    monkeypatch.setenv("DOCUMO_API_KEY", "doc_test_abc")
    from docstats.delivery.channels.fax import DocumoFaxChannel

    ch = DocumoFaxChannel()
    delivery = _FakeDelivery()

    async def _boom(*args, **kwargs):
        raise httpx.TimeoutException("took too long")

    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(side_effect=_boom)):
        with pytest.raises(DeliveryError) as exc:
            await ch.send(delivery, b"pdf")
    assert exc.value.error_code == "timeout"
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_fax_send_missing_id_in_response(monkeypatch):
    """Documo returned 2xx but no ``id`` / ``faxId`` — fatal, not retryable."""
    monkeypatch.setenv("DOCUMO_API_KEY", "doc_test_abc")
    from docstats.delivery.channels.fax import DocumoFaxChannel

    ch = DocumoFaxChannel()
    delivery = _FakeDelivery()

    mock_response = httpx.Response(
        200,
        json={"status": "ok"},  # no id field
        request=httpx.Request("POST", "https://api.documo.com/v1/fax/send"),
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(DeliveryError) as exc:
            await ch.send(delivery, b"pdf")
    assert exc.value.error_code == "vendor_bad_response"
    assert exc.value.retryable is False


# ---------- Documo webhook verifier ----------


def _documo_sig(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_documo_verify_valid():
    secret = "whs_test"
    body = b'{"event":"fax.delivered","data":{"id":"fx_1"}}'
    headers = {"X-Documo-Signature": _documo_sig(body, secret)}
    # Should not raise
    verify_documo(headers, body, secret)


def test_documo_verify_bare_hex_accepted():
    secret = "whs_test"
    body = b'{"event":"fax.delivered"}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    verify_documo({"X-Documo-Signature": sig}, body, secret)


def test_documo_verify_wrong_secret():
    body = b'{"event":"fax.delivered"}'
    headers = {"X-Documo-Signature": _documo_sig(body, "other")}
    with pytest.raises(DocumoVerificationError, match="mismatch"):
        verify_documo(headers, body, "correct")


def test_documo_verify_missing_header():
    with pytest.raises(DocumoVerificationError, match="Missing"):
        verify_documo({}, b"body", "secret")


def test_documo_verify_replayed_when_timestamp_present():
    import time

    secret = "s"
    body = b"{}"
    old_ts = str(int(time.time()) - 600)
    headers = {
        "X-Documo-Signature": _documo_sig(body, secret),
        "X-Documo-Timestamp": old_ts,
    }
    with pytest.raises(DocumoVerificationError, match="outside"):
        verify_documo(headers, body, secret)


def test_documo_verify_no_timestamp_no_replay_check():
    """Legacy Documo webhooks lack timestamp headers; signature alone suffices."""
    secret = "s"
    body = b"{}"
    headers = {"X-Documo-Signature": _documo_sig(body, secret)}
    verify_documo(headers, body, secret)


# ---------- Documo webhook route ----------


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


def test_documo_webhook_unknown_event_ignored(web_client):
    tc, _ = web_client
    resp = tc.post(
        "/webhooks/documo",
        json={"event": "fax.opened", "data": {"id": "fx_x"}},
    )
    assert resp.status_code == 200
    assert resp.json()["action"] == "ignored"


def test_documo_webhook_sent_updates_delivery(web_client):
    """Simulates: dispatcher's channel.send() already wrote a vendor_message_id,
    the Documo-side ``fax.sent`` webhook arrives afterwards (a race between
    sending→sent transitions).  The row should flip to ``sent`` status."""
    tc, storage = web_client
    user_id = storage.create_user("w@example.com", "hash")
    _, _, _, delivery = _seed(storage, user_id)
    # mark_delivery_sent writes sent_at + vendor_message_id; re-running for a
    # fax.sent event is idempotent on our side.
    storage.mark_delivery_sent(delivery.id, vendor_name="Documo", vendor_message_id="fx_sent_1")

    resp = tc.post(
        "/webhooks/documo",
        json={"event": "fax.sent", "data": {"id": "fx_sent_1"}},
    )
    assert resp.status_code == 200
    assert resp.json()["action"] == "updated"
    refreshed = storage.get_delivery(None, delivery.id)
    assert refreshed is not None
    assert refreshed.status == "sent"
    assert refreshed.vendor_message_id == "fx_sent_1"
    assert refreshed.vendor_name == "Documo"


def test_documo_webhook_delivered_updates_delivery(web_client):
    tc, storage = web_client
    user_id = storage.create_user("w@example.com", "hash")
    _, _, _, delivery = _seed(storage, user_id)
    storage.mark_delivery_sent(delivery.id, vendor_name="Documo", vendor_message_id="fx_deliv")

    resp = tc.post(
        "/webhooks/documo",
        json={"event": "fax.delivered", "data": {"id": "fx_deliv"}},
    )
    assert resp.status_code == 200
    refreshed = storage.get_delivery(None, delivery.id)
    assert refreshed is not None
    assert refreshed.status == "delivered"


def test_documo_webhook_failed_marks_failed(web_client):
    tc, storage = web_client
    user_id = storage.create_user("w@example.com", "hash")
    _, _, _, delivery = _seed(storage, user_id)
    storage.mark_delivery_sent(delivery.id, vendor_name="Documo", vendor_message_id="fx_fail")

    resp = tc.post(
        "/webhooks/documo",
        json={
            "event": "fax.failed",
            "data": {"id": "fx_fail", "reason": "no answer"},
        },
    )
    assert resp.status_code == 200
    refreshed = storage.get_delivery(None, delivery.id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.last_error_code == "fax_failed"


def test_documo_webhook_sending_is_informational(web_client):
    tc, storage = web_client
    user_id = storage.create_user("w@example.com", "hash")
    _, _, _, delivery = _seed(storage, user_id)
    # Dispatcher's channel.send() wrote the vendor_message_id; the
    # informational fax.sending webhook races behind it.
    storage.mark_delivery_sent(delivery.id, vendor_name="Documo", vendor_message_id="fx_s")

    resp = tc.post(
        "/webhooks/documo",
        json={"event": "fax.sending", "data": {"id": "fx_s"}},
    )
    assert resp.status_code == 200
    assert resp.json()["action"] == "informational"


def test_documo_webhook_unknown_delivery_ignored(web_client):
    tc, _ = web_client
    resp = tc.post(
        "/webhooks/documo",
        json={"event": "fax.delivered", "data": {"id": "fx_nonexistent"}},
    )
    assert resp.status_code == 200
    assert resp.json()["action"] == "unknown_delivery"


def test_documo_webhook_terminal_delivery_noop(web_client):
    """Cancelled rows are terminal — webhook must not reopen them."""
    tc, storage = web_client
    user_id = storage.create_user("w@example.com", "hash")
    scope, _, _, delivery = _seed(storage, user_id)
    storage.cancel_delivery(scope, delivery.id, cancelled_by_user_id=user_id)

    resp = tc.post(
        "/webhooks/documo",
        json={"event": "fax.delivered", "data": {"id": "fx_after_cancel"}},
    )
    assert resp.status_code == 200
    # cancel didn't set vendor_message_id, so the webhook can't match the row
    assert resp.json()["action"] == "unknown_delivery"


def test_documo_webhook_invalid_sig_rejected(web_client, monkeypatch):
    monkeypatch.setenv("DOCUMO_WEBHOOK_SECRET", "whs_prod")
    tc, _ = web_client

    resp = tc.post(
        "/webhooks/documo",
        content=b'{"event":"fax.delivered","data":{"id":"x"}}',
        headers={
            "Content-Type": "application/json",
            "X-Documo-Signature": "sha256=deadbeef",
        },
    )
    assert resp.status_code == 400


def test_documo_webhook_valid_sig_accepted(web_client, monkeypatch):
    monkeypatch.setenv("DOCUMO_WEBHOOK_SECRET", "whs_prod")
    tc, _ = web_client

    body = b'{"event":"fax.opened","data":{"id":"x"}}'
    resp = tc.post(
        "/webhooks/documo",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Documo-Signature": _documo_sig(body, "whs_prod"),
        },
    )
    assert resp.status_code == 200


# ---------- Send route integration ----------


def test_send_route_rejects_malformed_fax_recipient(tmp_path: Path):
    from fastapi.testclient import TestClient

    from docstats.auth import get_current_user
    from docstats.phi import CURRENT_PHI_CONSENT_VERSION
    from docstats.storage import Storage, get_storage
    from docstats.web import app

    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "hashed")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    scope, _, referral, _ = _seed(storage, user_id)

    def _fake_user():
        return {
            "id": user_id,
            "email": "a@example.com",
            "display_name": None,
            "first_name": "X",
            "last_name": "Y",
            "github_id": None,
            "github_login": None,
            "password_hash": "h",
            "created_at": "2026-01-01",
            "last_login_at": None,
            "terms_accepted_at": "2026-01-01",
            "phi_consent_at": "2026-01-01",
            "phi_consent_version": CURRENT_PHI_CONSENT_VERSION,
            "phi_consent_ip": None,
            "phi_consent_user_agent": None,
            "active_org_id": None,
        }

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = _fake_user
    try:
        import os as _os

        # Channel needs to be enabled so we reach the recipient validator
        _os.environ["DOCUMO_API_KEY"] = "doc_test"
        with TestClient(app, raise_server_exceptions=True) as tc:
            resp = tc.post(
                f"/referrals/{referral.id}/send",
                data={"channel": "fax", "recipient": "not-a-number"},
                follow_redirects=False,
            )
        assert resp.status_code == 422
        assert "US/Canada" in resp.text or "fax" in resp.text.lower()
    finally:
        app.dependency_overrides.clear()
        storage.close()
        import os as _os

        _os.environ.pop("DOCUMO_API_KEY", None)


def test_send_route_normalizes_fax_recipient(tmp_path: Path):
    from fastapi.testclient import TestClient

    from docstats.auth import get_current_user
    from docstats.phi import CURRENT_PHI_CONSENT_VERSION
    from docstats.storage import Storage, get_storage
    from docstats.web import app

    storage = Storage(db_path=tmp_path / "test.db")
    user_id = storage.create_user("a@example.com", "hashed")
    storage.record_phi_consent(
        user_id=user_id,
        phi_consent_version=CURRENT_PHI_CONSENT_VERSION,
        ip_address="",
        user_agent="",
    )
    scope, _, referral, _ = _seed(storage, user_id)

    def _fake_user():
        return {
            "id": user_id,
            "email": "a@example.com",
            "display_name": None,
            "first_name": "X",
            "last_name": "Y",
            "github_id": None,
            "github_login": None,
            "password_hash": "h",
            "created_at": "2026-01-01",
            "last_login_at": None,
            "terms_accepted_at": "2026-01-01",
            "phi_consent_at": "2026-01-01",
            "phi_consent_version": CURRENT_PHI_CONSENT_VERSION,
            "phi_consent_ip": None,
            "phi_consent_user_agent": None,
            "active_org_id": None,
        }

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = _fake_user
    try:
        import os as _os

        _os.environ["DOCUMO_API_KEY"] = "doc_test"
        with TestClient(app, raise_server_exceptions=True) as tc:
            resp = tc.post(
                f"/referrals/{referral.id}/send",
                data={"channel": "fax", "recipient": "(555) 555-9876"},
                follow_redirects=False,
            )
        assert resp.status_code == 303
        deliveries = storage.list_deliveries_for_referral(scope, referral.id)
        # newest is the one we just created; an earlier fixture one exists too
        assert any(d.recipient == "+15555559876" for d in deliveries)
    finally:
        app.dependency_overrides.clear()
        storage.close()
        import os as _os

        _os.environ.pop("DOCUMO_API_KEY", None)
