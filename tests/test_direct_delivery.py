"""Phase 9.D — DirectTrustChannel scaffolding tests.

Vendor-agnostic. Mocks the HISP HTTP layer; activation tests against a
real HISP land in a follow-up PR once the contract signs.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from docstats.delivery.base import (
    ChannelDisabledError,
    DeliveryError,
)
from docstats.validators import ValidationError, validate_direct_address


_REQUIRED_VARS = (
    "DIRECT_HISP_USERNAME",
    "DIRECT_HISP_PASSWORD",
    "DIRECT_HISP_ENDPOINT",
    "DIRECT_HISP_FROM_ADDRESS",
)


def _set_env(monkeypatch) -> None:
    monkeypatch.setenv("DIRECT_HISP_USERNAME", "hisp_user")
    monkeypatch.setenv("DIRECT_HISP_PASSWORD", "hisp_pass")
    monkeypatch.setenv("DIRECT_HISP_ENDPOINT", "https://hisp.example.com/api/send")
    monkeypatch.setenv("DIRECT_HISP_FROM_ADDRESS", "referrals@direct.referme.help")


class _FakeDelivery:
    """Minimal shape of ``Delivery`` that the direct channel touches."""

    def __init__(
        self,
        *,
        id: int = 1,
        referral_id: int = 7,
        recipient: str = "consult-intake@direct.heart-clinic.example.org",
        idempotency_key: str | None = "direct:abc",
        packet_artifact: dict | None = None,
    ) -> None:
        self.id = id
        self.referral_id = referral_id
        self.recipient = recipient
        self.idempotency_key = idempotency_key
        self.packet_artifact = packet_artifact or {}


# ---------- Direct address validator ----------


def test_validate_direct_address_accepts_basic_form():
    assert validate_direct_address("provider@direct.example.org") == "provider@direct.example.org"


def test_validate_direct_address_lowercases():
    assert validate_direct_address("Provider@Direct.Example.ORG") == "provider@direct.example.org"


def test_validate_direct_address_rejects_empty():
    with pytest.raises(ValidationError):
        validate_direct_address("")


def test_validate_direct_address_rejects_no_at_sign():
    with pytest.raises(ValidationError):
        validate_direct_address("not-an-email")


def test_validate_direct_address_rejects_bare_domain():
    with pytest.raises(ValidationError):
        validate_direct_address("user@nodot")


def test_validate_direct_address_rejects_overlong():
    with pytest.raises(ValidationError):
        validate_direct_address("a" * 250 + "@x.org")


# ---------- Channel factory + env-gating ----------


@pytest.mark.parametrize("missing_var", _REQUIRED_VARS)
def test_direct_channel_disabled_when_any_var_missing(monkeypatch, missing_var):
    _set_env(monkeypatch)
    monkeypatch.delenv(missing_var, raising=False)
    from docstats.delivery.channels.direct import DirectTrustChannel

    with pytest.raises(ChannelDisabledError, match=missing_var):
        DirectTrustChannel()


def test_direct_channel_disabled_when_all_vars_missing(monkeypatch):
    for v in _REQUIRED_VARS:
        monkeypatch.delenv(v, raising=False)
    from docstats.delivery.channels.direct import DirectTrustChannel

    with pytest.raises(ChannelDisabledError) as exc:
        DirectTrustChannel()
    # All four should appear in the reason
    for v in _REQUIRED_VARS:
        assert v in str(exc.value)


def test_direct_channel_instantiates_with_env(monkeypatch):
    _set_env(monkeypatch)
    from docstats.delivery.channels.direct import DirectTrustChannel

    ch = DirectTrustChannel()
    assert ch.name == "direct"
    assert ch.vendor_name == "DirectTrust HISP"  # default


def test_direct_channel_uses_custom_vendor_name(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("DIRECT_HISP_VENDOR", "DataMotion")
    from docstats.delivery.channels.direct import DirectTrustChannel

    assert DirectTrustChannel().vendor_name == "DataMotion"


def test_direct_absent_from_registry_when_vars_missing(monkeypatch):
    for v in _REQUIRED_VARS:
        monkeypatch.delenv(v, raising=False)
    from docstats.delivery.registry import enabled_channels

    assert "direct" not in enabled_channels()


def test_direct_present_in_registry_when_vars_set(monkeypatch):
    _set_env(monkeypatch)
    from docstats.delivery.registry import enabled_channels

    assert "direct" in enabled_channels()


# ---------- send() happy path + error paths ----------


@pytest.mark.asyncio
async def test_direct_send_success(monkeypatch):
    _set_env(monkeypatch)
    from docstats.delivery.channels.direct import DirectTrustChannel

    ch = DirectTrustChannel()
    delivery = _FakeDelivery(packet_artifact={"direct_subject": "Cardiology referral"})

    mock_response = httpx.Response(
        202,
        json={"id": "msg_abc123", "status": "queued"},
        request=httpx.Request("POST", "https://hisp.example.com/api/send"),
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_response)) as post:
        receipt = await ch.send(delivery, b"%PDF-1.4\n<fake>")

    assert receipt.vendor_name == "DirectTrust HISP"
    assert receipt.vendor_message_id == "msg_abc123"
    assert receipt.status == "sent"

    call = post.call_args
    headers = call.kwargs["headers"]
    expected_basic = "Basic " + base64.b64encode(b"hisp_user:hisp_pass").decode("ascii")
    assert headers["Authorization"] == expected_basic
    assert headers["Idempotency-Key"] == "direct:abc"

    data = call.kwargs["data"]
    assert data["from"] == "referrals@direct.referme.help"
    assert data["to"] == "consult-intake@direct.heart-clinic.example.org"
    assert data["subject"] == "Cardiology referral"
    assert data["body"]  # default body present

    files = call.kwargs["files"]
    assert files["files"][0] == "referral-7.pdf"
    assert files["files"][2] == "application/pdf"


@pytest.mark.asyncio
async def test_direct_send_accepts_messageid_field(monkeypatch):
    """Some HISPs return ``messageId`` instead of ``id``."""
    _set_env(monkeypatch)
    from docstats.delivery.channels.direct import DirectTrustChannel

    ch = DirectTrustChannel()
    delivery = _FakeDelivery()
    mock_response = httpx.Response(
        200,
        json={"messageId": "alt_456"},
        request=httpx.Request("POST", "https://hisp.example.com/api/send"),
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_response)):
        receipt = await ch.send(delivery, b"pdf")

    assert receipt.vendor_message_id == "alt_456"


@pytest.mark.asyncio
async def test_direct_send_uses_bearer_token_when_configured(monkeypatch):
    """Bearer mode reads DIRECT_HISP_TOKEN, not DIRECT_HISP_USERNAME — putting the
    token in USERNAME used to silently work and made operator setup confusing."""
    monkeypatch.setenv("DIRECT_HISP_ENDPOINT", "https://hisp.example.com/api/send")
    monkeypatch.setenv("DIRECT_HISP_FROM_ADDRESS", "referrals@direct.referme.help")
    monkeypatch.setenv("DIRECT_HISP_AUTH_SCHEME", "bearer")
    monkeypatch.setenv("DIRECT_HISP_TOKEN", "tok_xyz")
    monkeypatch.delenv("DIRECT_HISP_USERNAME", raising=False)
    monkeypatch.delenv("DIRECT_HISP_PASSWORD", raising=False)
    from docstats.delivery.channels.direct import DirectTrustChannel

    ch = DirectTrustChannel()
    delivery = _FakeDelivery()
    mock_response = httpx.Response(
        200,
        json={"id": "msg_x"},
        request=httpx.Request("POST", "https://hisp.example.com/api/send"),
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_response)) as post:
        await ch.send(delivery, b"pdf")
    headers = post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok_xyz"


def test_direct_bearer_mode_requires_token_not_username(monkeypatch):
    """Bearer scheme without DIRECT_HISP_TOKEN must fail closed, not silently
    fall back to using USERNAME as the token."""
    monkeypatch.setenv("DIRECT_HISP_ENDPOINT", "https://hisp.example.com/api/send")
    monkeypatch.setenv("DIRECT_HISP_FROM_ADDRESS", "referrals@direct.referme.help")
    monkeypatch.setenv("DIRECT_HISP_AUTH_SCHEME", "bearer")
    monkeypatch.setenv("DIRECT_HISP_USERNAME", "hisp_user")  # present but irrelevant
    monkeypatch.delenv("DIRECT_HISP_TOKEN", raising=False)
    from docstats.delivery.channels.direct import DirectTrustChannel

    with pytest.raises(ChannelDisabledError, match="DIRECT_HISP_TOKEN"):
        DirectTrustChannel()


def test_direct_bearer_mode_does_not_require_password(monkeypatch):
    """Bearer mode skips USERNAME/PASSWORD requirements — operator only needs
    a token. Earlier scaffolding required PASSWORD even in bearer mode."""
    monkeypatch.setenv("DIRECT_HISP_ENDPOINT", "https://hisp.example.com/api/send")
    monkeypatch.setenv("DIRECT_HISP_FROM_ADDRESS", "referrals@direct.referme.help")
    monkeypatch.setenv("DIRECT_HISP_AUTH_SCHEME", "bearer")
    monkeypatch.setenv("DIRECT_HISP_TOKEN", "tok")
    monkeypatch.delenv("DIRECT_HISP_USERNAME", raising=False)
    monkeypatch.delenv("DIRECT_HISP_PASSWORD", raising=False)
    from docstats.delivery.channels.direct import DirectTrustChannel

    DirectTrustChannel()  # should not raise


def test_direct_invalid_auth_scheme_fails_closed(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("DIRECT_HISP_AUTH_SCHEME", "oauth1")
    from docstats.delivery.channels.direct import DirectTrustChannel

    with pytest.raises(ChannelDisabledError, match="basic.*bearer"):
        DirectTrustChannel()


def test_direct_vendor_name_has_class_level_default():
    """Mirrors email/fax precedent — Class.vendor_name is introspectable
    before instantiation, so UI labels / health checks work without needing
    a configured channel."""
    from docstats.delivery.channels.direct import DirectTrustChannel

    assert DirectTrustChannel.vendor_name == "DirectTrust HISP"


@pytest.mark.asyncio
async def test_direct_send_empty_packet_is_fatal(monkeypatch):
    _set_env(monkeypatch)
    from docstats.delivery.channels.direct import DirectTrustChannel

    ch = DirectTrustChannel()
    with pytest.raises(DeliveryError) as exc:
        await DirectTrustChannel().send(_FakeDelivery(), b"")
    assert exc.value.error_code == "empty_packet"
    assert exc.value.retryable is False
    del ch  # silence unused-var lint


@pytest.mark.asyncio
async def test_direct_send_429_retryable(monkeypatch):
    _set_env(monkeypatch)
    from docstats.delivery.channels.direct import DirectTrustChannel

    mock_response = httpx.Response(
        429,
        text="rate limited",
        request=httpx.Request("POST", "https://hisp.example.com/api/send"),
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(DeliveryError) as exc:
            await DirectTrustChannel().send(_FakeDelivery(), b"pdf")
    assert exc.value.error_code == "rate_limited"
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_direct_send_5xx_retryable(monkeypatch):
    _set_env(monkeypatch)
    from docstats.delivery.channels.direct import DirectTrustChannel

    mock_response = httpx.Response(
        503,
        text="HISP down",
        request=httpx.Request("POST", "https://hisp.example.com/api/send"),
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(DeliveryError) as exc:
            await DirectTrustChannel().send(_FakeDelivery(), b"pdf")
    assert exc.value.error_code == "vendor_5xx"
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_direct_send_4xx_fatal(monkeypatch):
    _set_env(monkeypatch)
    from docstats.delivery.channels.direct import DirectTrustChannel

    mock_response = httpx.Response(
        400,
        text="bad address",
        request=httpx.Request("POST", "https://hisp.example.com/api/send"),
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(DeliveryError) as exc:
            await DirectTrustChannel().send(_FakeDelivery(), b"pdf")
    assert exc.value.error_code == "vendor_4xx"
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_direct_send_timeout_retryable(monkeypatch):
    _set_env(monkeypatch)
    from docstats.delivery.channels.direct import DirectTrustChannel

    async def _timeout(*args, **kwargs):
        raise httpx.TimeoutException("read timeout")

    with patch.object(httpx.AsyncClient, "post", new=_timeout):
        with pytest.raises(DeliveryError) as exc:
            await DirectTrustChannel().send(_FakeDelivery(), b"pdf")
    assert exc.value.error_code == "timeout"
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_direct_send_network_retryable(monkeypatch):
    _set_env(monkeypatch)
    from docstats.delivery.channels.direct import DirectTrustChannel

    async def _net(*args, **kwargs):
        raise httpx.ConnectError("DNS failure")

    with patch.object(httpx.AsyncClient, "post", new=_net):
        with pytest.raises(DeliveryError) as exc:
            await DirectTrustChannel().send(_FakeDelivery(), b"pdf")
    assert exc.value.error_code == "network_error"
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_direct_send_missing_message_id_fatal(monkeypatch):
    _set_env(monkeypatch)
    from docstats.delivery.channels.direct import DirectTrustChannel

    mock_response = httpx.Response(
        200,
        json={"status": "queued"},  # no id field
        request=httpx.Request("POST", "https://hisp.example.com/api/send"),
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(DeliveryError) as exc:
            await DirectTrustChannel().send(_FakeDelivery(), b"pdf")
    # Match email/fax convention: shape-violating success responses bucket
    # under vendor_bad_response, not a separate per-cause code.
    assert exc.value.error_code == "vendor_bad_response"
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_direct_send_non_json_fatal(monkeypatch):
    _set_env(monkeypatch)
    from docstats.delivery.channels.direct import DirectTrustChannel

    mock_response = httpx.Response(
        200,
        text="<html>not json</html>",
        request=httpx.Request("POST", "https://hisp.example.com/api/send"),
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(DeliveryError) as exc:
            await DirectTrustChannel().send(_FakeDelivery(), b"pdf")
    assert exc.value.error_code == "vendor_bad_response"
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_direct_send_4xx_redacts_credentials_and_from_address(monkeypatch):
    """HISPs sometimes echo the auth credential or sender Direct address in
    error bodies. The channel docstring promises no PHI in logs, so the
    response excerpt that lands in DeliveryError must scrub both."""
    _set_env(monkeypatch)
    from docstats.delivery.channels.direct import DirectTrustChannel

    leaky_body = (
        "Auth failed for user hisp_user with password hisp_pass; "
        "from=referrals@direct.referme.help; please contact support."
    )
    mock_response = httpx.Response(
        401,
        text=leaky_body,
        request=httpx.Request("POST", "https://hisp.example.com/api/send"),
    )
    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(DeliveryError) as exc:
            await DirectTrustChannel().send(_FakeDelivery(), b"pdf")
    msg = str(exc.value)
    assert "hisp_user" not in msg
    assert "hisp_pass" not in msg
    assert "referrals@direct.referme.help" not in msg
    assert "[REDACTED:credential]" in msg
    assert "[REDACTED:from-address]" in msg


@pytest.mark.asyncio
async def test_direct_poll_status_returns_none(monkeypatch):
    _set_env(monkeypatch)
    from docstats.delivery.channels.direct import DirectTrustChannel

    result = await DirectTrustChannel().poll_status(_FakeDelivery())
    assert result is None
