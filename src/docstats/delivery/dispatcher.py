"""DB-backed delivery dispatcher — the only thing that actually sends packets.

Runs as a lifespan-managed asyncio task. Every ``DELIVERY_DISPATCHER_INTERVAL_SECONDS``
(default 10s), it polls ``deliveries`` for rows the sweeper should pick up:

- ``status = 'queued'`` — newly enqueued, dispatch immediately.
- ``status = 'sending'`` AND ``updated_at < now() - stuck_threshold`` —
  a previous dispatcher crashed or got SIGTERM'd mid-send. Retry.

For each row:

1. Flip to ``sending`` + bump ``updated_at``; record a new
   ``delivery_attempts`` row with ``result='in_progress'``.
2. Load the parent referral + patient. Render the packet. Call
   ``Channel.send(delivery, packet_bytes)``.
3. Success → flip ``sent`` (or ``delivered``), stamp ``sent_at`` /
   ``delivered_at``, record ``vendor_message_id``, close the attempt row
   with ``result='success'``. Emit ``referral_events.dispatched``.
4. Retryable failure (``DeliveryError.retryable=True``) → bump
   ``retry_count``, record ``last_error_*``, keep status ``queued``,
   close the attempt row with ``result='retryable'``. If retry_count
   hits the cap, flip to ``failed`` and emit ``delivery_failed`` event.
5. Fatal failure (``retryable=False``, incl. ``ChannelDisabledError``) →
   flip to ``failed`` immediately, close attempt with ``result='fatal'``,
   emit ``delivery_failed``.

The dispatcher honors ``async_limiter()`` (default 5 concurrent) so no
more than N vendor calls run at once. SIGTERM / lifespan shutdown flips
an internal event; the current iteration completes gracefully.

Railway-specific note: this module doesn't install signal handlers
directly — FastAPI's lifespan manager handles SIGTERM by cancelling the
dispatcher task. The ``asyncio.shield`` around the per-row work lets the
iteration finish before the outer ``CancelledError`` propagates.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Awaitable, Callable

from docstats.delivery.base import Channel, ChannelDisabledError, DeliveryError
from docstats.delivery.registry import get_channel

if TYPE_CHECKING:
    from docstats.domain.deliveries import Delivery
    from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

# Tunable via env var. Low default so queued deliveries dispatch quickly
# in manual testing; prod may want 30s to lower baseline DB load.
DEFAULT_INTERVAL_SECONDS = 10
DEFAULT_STUCK_SENDING_SECONDS = 120  # sending rows older than this are retried
DEFAULT_MAX_RETRIES = 5
DEFAULT_BATCH_SIZE = 20


def _get_interval_seconds() -> int:
    raw = os.environ.get("DELIVERY_DISPATCHER_INTERVAL_SECONDS")
    if raw and raw.isdigit():
        return max(1, min(int(raw), 300))
    return DEFAULT_INTERVAL_SECONDS


def _get_max_retries() -> int:
    raw = os.environ.get("DELIVERY_MAX_RETRIES")
    if raw and raw.isdigit():
        return max(0, min(int(raw), 20))
    return DEFAULT_MAX_RETRIES


async def _process_one(
    storage: "StorageBase",
    delivery: "Delivery",
    channel_factory: Callable[[str], Channel],
    render_packet: Callable[["Delivery"], Awaitable[bytes]],
) -> None:
    """Process one delivery row end-to-end.

    Extracted from the main loop so tests can drive a single iteration
    without standing up the full sweeper coroutine.
    """
    from docstats.domain.deliveries import (
        truncate_error_message,
        truncate_vendor_excerpt,
    )

    attempt_number = delivery.retry_count + 1
    attempt_id = storage.record_delivery_attempt_start(
        delivery_id=delivery.id,
        attempt_number=attempt_number,
    )
    storage.mark_delivery_sending(delivery.id)

    try:
        channel = channel_factory(delivery.channel)
        packet_bytes = await render_packet(delivery)
        receipt = await channel.send(delivery, packet_bytes)
    except ChannelDisabledError as e:
        storage.record_delivery_attempt_complete(
            attempt_id=attempt_id,
            result="fatal",
            error_code=e.error_code,
            error_message=truncate_error_message(str(e)),
        )
        storage.mark_delivery_failed(
            delivery.id,
            error_code=e.error_code,
            error_message=truncate_error_message(str(e)),
        )
        try:
            _emit_referral_event(storage, delivery, "delivery_failed", note=e.error_code)
        except Exception:
            logger.exception("Failed to emit delivery_failed event for delivery %s", delivery.id)
        return
    except DeliveryError as e:
        max_retries = _get_max_retries()
        hit_cap = attempt_number >= max_retries and e.retryable
        result = "fatal" if not e.retryable else ("retryable" if not hit_cap else "fatal")
        storage.record_delivery_attempt_complete(
            attempt_id=attempt_id,
            result=result,
            error_code=e.error_code,
            error_message=truncate_error_message(str(e)),
        )
        if not e.retryable or hit_cap:
            storage.mark_delivery_failed(
                delivery.id,
                error_code=e.error_code,
                error_message=truncate_error_message(str(e)),
            )
            try:
                _emit_referral_event(storage, delivery, "delivery_failed", note=e.error_code)
            except Exception:
                logger.exception(
                    "Failed to emit delivery_failed event for delivery %s", delivery.id
                )
        else:
            storage.requeue_delivery_for_retry(
                delivery.id,
                error_code=e.error_code,
                error_message=truncate_error_message(str(e)),
            )
        return
    except Exception as e:
        # Unexpected exception from the channel — treat as retryable by
        # default, but respect the retry cap. This is the "programming
        # bug in the channel impl" catch-all; we never want a stuck row.
        max_retries = _get_max_retries()
        hit_cap = attempt_number >= max_retries
        storage.record_delivery_attempt_complete(
            attempt_id=attempt_id,
            result="retryable" if not hit_cap else "fatal",
            error_code="unexpected",
            error_message=truncate_error_message(f"{type(e).__name__}: {e}"),
        )
        if hit_cap:
            storage.mark_delivery_failed(
                delivery.id,
                error_code="unexpected",
                error_message=truncate_error_message(f"{type(e).__name__}: {e}"),
            )
            try:
                _emit_referral_event(storage, delivery, "delivery_failed", note="unexpected")
            except Exception:
                logger.exception(
                    "Failed to emit delivery_failed event for delivery %s", delivery.id
                )
        else:
            storage.requeue_delivery_for_retry(
                delivery.id,
                error_code="unexpected",
                error_message=truncate_error_message(f"{type(e).__name__}: {e}"),
            )
        logger.exception("Unexpected error processing delivery %s", delivery.id)
        return

    # Success path.
    storage.record_delivery_attempt_complete(
        attempt_id=attempt_id,
        result="success",
        vendor_response_excerpt=truncate_vendor_excerpt(receipt.vendor_response_excerpt),
    )
    storage.mark_delivery_sent(
        delivery.id,
        vendor_name=receipt.vendor_name,
        vendor_message_id=receipt.vendor_message_id,
        status=receipt.status,  # "sent" or "delivered"
    )
    try:
        note = receipt.vendor_message_id
        event_type = "delivered" if receipt.status == "delivered" else "dispatched"
        _emit_referral_event(storage, delivery, event_type, note=note)
    except Exception:
        logger.exception("Failed to emit delivery event for delivery %s", delivery.id)


def _emit_referral_event(
    storage: "StorageBase",
    delivery: "Delivery",
    event_type: str,
    *,
    note: str | None = None,
) -> None:
    """Write a ``referral_events`` row for a delivery state change.

    Scope comes from the delivery's denormalized scope columns — the
    dispatcher never has a user session, so it builds a Scope from the
    row's own data.
    """
    from docstats.scope import Scope

    scope = Scope(
        user_id=delivery.scope_user_id,
        organization_id=delivery.scope_organization_id,
        membership_role=None,
    )
    storage.record_referral_event(
        scope,
        delivery.referral_id,
        event_type=event_type,
        note=note,
        actor_user_id=None,  # dispatcher has no actor
    )


async def _run_iteration(
    storage: "StorageBase",
    channel_factory: Callable[[str], Channel],
    render_packet: Callable[["Delivery"], Awaitable[bytes]],
    limiter: asyncio.Semaphore,
) -> int:
    """One sweep: fetch ready rows, dispatch each under the limiter.

    Returns the number of rows processed (for tests + /health endpoint).
    """
    ready = storage.list_deliveries_ready_for_dispatch(
        limit=DEFAULT_BATCH_SIZE,
        stuck_sending_seconds=DEFAULT_STUCK_SENDING_SECONDS,
    )
    if not ready:
        return 0

    async def _bounded(delivery: "Delivery") -> None:
        async with limiter:
            await _process_one(storage, delivery, channel_factory, render_packet)

    # asyncio.shield on each so the outer cancel doesn't kill an
    # in-progress attempt (we want to finish the vendor call, even if
    # it means one last iteration after SIGTERM).
    await asyncio.gather(*(asyncio.shield(_bounded(d)) for d in ready), return_exceptions=True)
    return len(ready)


async def run(
    storage: "StorageBase",
    *,
    render_packet: Callable[["Delivery"], Awaitable[bytes]],
    stop_event: asyncio.Event,
    channel_factory: Callable[[str], Channel] | None = None,
    interval_seconds: int | None = None,
) -> None:
    """Main dispatcher loop. Runs until ``stop_event`` is set.

    ``render_packet`` is a callable the caller wires to the real
    ``exports.render_packet`` (or a stub in tests). Injecting it keeps
    this module free of storage/exports import cycles.

    ``stop_event`` is the shutdown signal. The lifespan manager in
    ``web.py`` sets it on shutdown; the current iteration completes
    before the loop exits.

    Never raises. All exceptions are logged and swallowed so a bad row
    or vendor hiccup doesn't kill the dispatcher.
    """
    from docstats.concurrency import async_limiter

    limiter = async_limiter()
    factory = channel_factory or get_channel
    interval = interval_seconds or _get_interval_seconds()

    logger.info("Delivery dispatcher started (interval=%ds)", interval)
    while not stop_event.is_set():
        try:
            processed = await _run_iteration(storage, factory, render_packet, limiter)
            if processed:
                logger.info("Delivery dispatcher processed %d row(s)", processed)
        except Exception:
            logger.exception("Delivery dispatcher iteration raised")
        try:
            # Wait for the interval OR the stop event, whichever is
            # sooner. asyncio.wait_for with TimeoutError is the
            # idiomatic way to race a sleep against an event.
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
    logger.info("Delivery dispatcher shutting down")
