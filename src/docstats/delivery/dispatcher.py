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
import random
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

# Exponential backoff schedule for retryable failures — Phase 9.E.
# Sweeper doesn't delay the requeue itself (it just flips the row back to
# ``queued`` with a bumped retry_count); the schedule instead governs how
# long the ROW sits in ``sending`` before the next pickup, via the sweeper's
# ``stuck_sending_seconds`` window.  The schedule is consulted when the
# dispatcher decides whether a given pickup is "too early" for the current
# retry_count — see ``_next_retry_at``.  Tuning 10s → 30s → 2min → 10min → 1h.
_BACKOFF_SECONDS: tuple[int, ...] = (10, 30, 120, 600, 3600)
_BACKOFF_JITTER_PCT = 0.15  # ±15% jitter so stampedes don't align across retries


def _backoff_seconds(retry_count: int) -> int:
    """Return the minimum seconds to wait before the ``retry_count``-th retry.

    ``retry_count=0`` → 0 (first try is immediate).  Beyond the table's end
    we clamp to the final value (1h).  A uniformly-distributed ±15% jitter is
    applied so N deliveries hitting 429 at the same time don't all retry in
    lockstep.
    """
    if retry_count <= 0:
        return 0
    idx = min(retry_count - 1, len(_BACKOFF_SECONDS) - 1)
    base = _BACKOFF_SECONDS[idx]
    jitter = int(base * _BACKOFF_JITTER_PCT * (random.random() * 2 - 1))
    return max(0, base + jitter)


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


def _get_stuck_sending_seconds() -> int:
    raw = os.environ.get("DELIVERY_STUCK_SENDING_SECONDS")
    if raw and raw.isdigit():
        return max(30, min(int(raw), 3600))
    return DEFAULT_STUCK_SENDING_SECONDS


# ---- Health snapshot (Phase 9.E) ----
#
# The sweeper coroutine writes a fresh SweepStats each iteration; the admin
# health route reads the latest value.  Access is guarded by a lock because
# the sweeper runs in the main event loop while FastAPI handlers may touch
# the snapshot from any worker.  No DB involvement — this is purely process-
# local state; multi-worker deployments see only the worker that served the
# request.


@dataclass
class SweepStats:
    last_sweep_at: datetime | None = None
    last_sweep_duration_seconds: float | None = None
    last_sweep_processed: int = 0
    total_iterations: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None
    running: bool = False
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    started_at: datetime | None = None


@dataclass
class _SweepStatsHolder:
    stats: SweepStats = field(default_factory=SweepStats)
    lock: threading.Lock = field(default_factory=threading.Lock)


_STATS_HOLDER = _SweepStatsHolder()


def get_sweep_stats() -> SweepStats:
    """Return a snapshot of the current sweeper stats.  Thread-safe."""
    with _STATS_HOLDER.lock:
        s = _STATS_HOLDER.stats
        return SweepStats(
            last_sweep_at=s.last_sweep_at,
            last_sweep_duration_seconds=s.last_sweep_duration_seconds,
            last_sweep_processed=s.last_sweep_processed,
            total_iterations=s.total_iterations,
            last_error=s.last_error,
            last_error_at=s.last_error_at,
            running=s.running,
            interval_seconds=s.interval_seconds,
            started_at=s.started_at,
        )


def _record_sweep(
    *,
    processed: int,
    duration_seconds: float,
    error: str | None = None,
) -> None:
    with _STATS_HOLDER.lock:
        s = _STATS_HOLDER.stats
        s.last_sweep_at = datetime.now(tz=timezone.utc)
        s.last_sweep_duration_seconds = duration_seconds
        s.last_sweep_processed = processed
        s.total_iterations += 1
        if error is not None:
            s.last_error = error
            s.last_error_at = s.last_sweep_at


def _set_running(running: bool, *, interval_seconds: int | None = None) -> None:
    with _STATS_HOLDER.lock:
        s = _STATS_HOLDER.stats
        s.running = running
        if running and s.started_at is None:
            s.started_at = datetime.now(tz=timezone.utc)
        if not running:
            s.started_at = None
        if interval_seconds is not None:
            s.interval_seconds = interval_seconds


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


def _should_skip_for_backoff(delivery: "Delivery") -> bool:
    """Return True iff this row's last-update timestamp is still within
    the exponential-backoff window for its current retry_count.

    Rows that were just flipped back to ``queued`` by a retryable failure
    carry a bumped retry_count + a fresh ``updated_at``; we consult
    ``_backoff_seconds(retry_count)`` to decide whether the sweeper should
    pick them up yet.  Freshly-enqueued rows (retry_count=0) have no backoff
    and always dispatch.
    """
    if delivery.retry_count <= 0:
        return False
    backoff = _backoff_seconds(delivery.retry_count)
    if backoff <= 0:
        return False
    now = datetime.now(tz=timezone.utc)
    updated = delivery.updated_at
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    elapsed = (now - updated).total_seconds()
    return elapsed < backoff


async def _run_iteration(
    storage: "StorageBase",
    channel_factory: Callable[[str], Channel],
    render_packet: Callable[["Delivery"], Awaitable[bytes]],
    limiter: asyncio.Semaphore,
) -> int:
    """One sweep: fetch ready rows, dispatch each under the limiter.

    Returns the number of rows processed (for tests + /health endpoint).
    """
    stuck_seconds = _get_stuck_sending_seconds()
    ready = storage.list_deliveries_ready_for_dispatch(
        limit=DEFAULT_BATCH_SIZE,
        stuck_sending_seconds=stuck_seconds,
    )
    if not ready:
        return 0

    # Honor the exponential backoff: skip rows whose retry window hasn't
    # elapsed.  The row stays in ``queued`` so the next iteration picks it
    # up once enough time has passed.
    dispatch: list["Delivery"] = [d for d in ready if not _should_skip_for_backoff(d)]
    if not dispatch:
        return 0

    async def _bounded(delivery: "Delivery") -> None:
        async with limiter:
            await _process_one(storage, delivery, channel_factory, render_packet)

    # asyncio.shield on each so the outer cancel doesn't kill an
    # in-progress attempt (we want to finish the vendor call, even if
    # it means one last iteration after SIGTERM).
    await asyncio.gather(*(asyncio.shield(_bounded(d)) for d in dispatch), return_exceptions=True)
    return len(dispatch)


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

    _set_running(True, interval_seconds=interval)
    logger.info(
        "Delivery dispatcher started",
        extra={"interval_seconds": interval, "max_retries": _get_max_retries()},
    )
    try:
        while not stop_event.is_set():
            loop = asyncio.get_event_loop()
            start = loop.time()
            processed = 0
            iteration_error: str | None = None
            try:
                processed = await _run_iteration(storage, factory, render_packet, limiter)
                if processed:
                    logger.info(
                        "Delivery dispatcher processed %d row(s)",
                        processed,
                        extra={"processed": processed},
                    )
            except Exception as exc:
                iteration_error = f"{type(exc).__name__}: {exc}"[:500]
                logger.exception("Delivery dispatcher iteration raised")
            finally:
                _record_sweep(
                    processed=processed,
                    duration_seconds=max(0.0, loop.time() - start),
                    error=iteration_error,
                )
            try:
                # Wait for the interval OR the stop event, whichever is
                # sooner. asyncio.wait_for with TimeoutError is the
                # idiomatic way to race a sleep against an event.
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
    finally:
        _set_running(False)
    logger.info("Delivery dispatcher shutting down")
