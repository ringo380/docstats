"""Outbound delivery — Phase 9.

Channel abstraction + DB-backed retry sweeper. No I/O in this package
module itself — each channel impl lives under ``channels/`` and pulls in
its own httpx client as needed.

Phase 9 intentionally ships without ``asyncio.create_task`` from route
handlers. Railway sends SIGTERM during deploys; orphaned tasks leave
delivery rows stuck in ``queued``. The DB-backed dispatcher
(``dispatcher.run``) is the single recovery mechanism and must be
running in the FastAPI lifespan for delivery to work at all.
"""

from __future__ import annotations

from docstats.delivery.base import (
    Channel,
    ChannelDisabledError,
    DeliveryError,
    DeliveryReceipt,
    DeliveryStatus,
)
from docstats.delivery.registry import enabled_channels, get_channel

__all__ = [
    "Channel",
    "ChannelDisabledError",
    "DeliveryError",
    "DeliveryReceipt",
    "DeliveryStatus",
    "enabled_channels",
    "get_channel",
]
