"""Channel protocol + shared dataclasses for Phase 9 delivery.

Every channel impl (email, fax, direct) implements :class:`Channel`. The
dispatcher calls ``send()`` once per delivery attempt and ``poll_status()``
only if the channel explicitly supports vendor-initiated status queries
(most use webhook callbacks instead). When a channel is not configured
(missing env vars, BAA not signed), ``send()`` raises
:class:`ChannelDisabledError` and the dispatcher flips the row to
``failed`` with ``error_code = "channel_disabled"``.

This module is pure Python — no FastAPI, no storage. Channel impls
pull in `httpx` and their own HTTP retry helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from docstats.domain.deliveries import Delivery


@dataclass(frozen=True)
class DeliveryReceipt:
    """Return value from a successful ``Channel.send()``.

    The channel has handed the packet to the vendor, who has accepted it
    and assigned a ``vendor_message_id`` we'll use to correlate
    webhook callbacks back to the delivery row.

    ``status`` is ``"sent"`` when the vendor accepted for processing but
    hasn't yet confirmed end-recipient delivery (e.g. Resend's ``email.sent``
    vs ``email.delivered``). ``"delivered"`` is reserved for channels that
    synchronously confirm end-recipient receipt — rare in practice.
    """

    vendor_name: str
    vendor_message_id: str
    status: str = "sent"  # "sent" | "delivered"
    vendor_response_excerpt: str | None = None


@dataclass(frozen=True)
class DeliveryStatus:
    """Return value from ``Channel.poll_status()``.

    Not all channels support polling — most use webhook-driven updates.
    Poll is primarily for debugging stuck rows.
    """

    status: str  # one of DELIVERY_STATUS_VALUES
    vendor_response_excerpt: str | None = None


class DeliveryError(Exception):
    """Base for all Channel errors.

    Attribute ``error_code`` is a short stable token (e.g.
    ``"channel_disabled"`` / ``"invalid_recipient"`` / ``"vendor_5xx"``)
    that lands in ``deliveries.last_error_code`` for operator triage.
    ``retryable=True`` means the dispatcher should retry with backoff;
    ``False`` means the row transitions directly to ``failed``.
    """

    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


class ChannelDisabledError(DeliveryError):
    """Raised when a channel has no valid credentials / feature flag.

    The registry raises this when ``get_channel(name)`` is called for a
    channel whose env vars aren't set. Never retryable — flipping the
    env var is an admin action, not a transient condition.
    """

    def __init__(self, channel: str, reason: str = "credentials not configured") -> None:
        super().__init__(
            error_code="channel_disabled",
            message=f"Channel {channel!r} is disabled: {reason}",
            retryable=False,
        )
        self.channel = channel


class Channel(Protocol):
    """Interface every delivery channel implements.

    Channels are stateless — the dispatcher instantiates a fresh instance
    per delivery attempt (channels can cache httpx clients at module
    level if needed; see ``channels/email.py`` for the pattern).
    """

    name: str  # "fax" | "email" | "direct"
    vendor_name: str  # "Documo" | "Resend" | "DataMotion" | ...

    async def send(self, delivery: "Delivery", packet_bytes: bytes) -> DeliveryReceipt:
        """Submit the packet to the vendor. Returns a receipt on success.

        Raises :class:`DeliveryError` (or subclass) on failure.
        """
        ...

    async def poll_status(self, delivery: "Delivery") -> DeliveryStatus | None:
        """Optional: query the vendor for the current status.

        Return ``None`` for channels that don't support polling. Most
        channels use webhook callbacks and rely on those for status
        updates — the dispatcher only calls this as a recovery path
        when a delivery is stuck in ``sent`` past a staleness threshold.
        """
        ...
