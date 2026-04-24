"""Attachment retention sweep — Phase 10.C.

Runs as a lifespan-managed asyncio task alongside the delivery dispatcher
(Phase 9.A).  Every ``ATTACHMENT_RETENTION_INTERVAL_SECONDS`` (default
86400 = 24h), the sweep:

1. Enumerates every live organization plus every solo user that owns
   bucket-backed attachments.
2. For each tenant, computes ``cutoff = now - retention_days`` (org
   retention from ``organizations.attachment_retention_days``; solo users
   get :data:`DEFAULT_ATTACHMENT_RETENTION_DAYS`).
3. Pulls a batch of expired attachments via
   :meth:`StorageBase.list_attachments_expired`.
4. For each row: deletes the bucket object (best-effort), hard-deletes
   the DB row, emits ``attachment.purged`` audit.
5. Repeats per-tenant until the batch returns empty (bounded by
   ``max_batches_per_tenant`` so one giant tenant can't starve peers).

Design notes
------------
- Hard-delete is final; ``referral_attachments`` rows are not soft-deleted.
  The ``attachment.purged`` audit is the evidence trail.
- Bucket delete is best-effort — if the bucket call fails, the DB row
  stays so the next sweep retries.  Matches Phase 10.A's pattern for
  user-initiated deletes.
- Thread-safe stats snapshot mirrors Phase 9.E's ``SweepStats`` so the
  admin health panel can surface retention state next to the delivery
  dispatcher stats (wiring lands in a follow-up).
- The sweep is disabled under tests via
  ``DOCSTATS_SKIP_ATTACHMENT_RETENTION=1`` (same pattern as
  ``DOCSTATS_SKIP_DELIVERY_DISPATCHER``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from docstats.domain.audit import record as audit_record
from docstats.domain.orgs import DEFAULT_ATTACHMENT_RETENTION_DAYS
from docstats.storage_files.base import StorageFileError

if TYPE_CHECKING:
    from docstats.domain.referrals import ReferralAttachment
    from docstats.storage_base import StorageBase
    from docstats.storage_files.base import StorageFileBackend

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 86400  # 24h
DEFAULT_BATCH_SIZE = 500
DEFAULT_MAX_BATCHES_PER_TENANT = 10  # 5000 rows per tenant per sweep, upper bound


def _get_interval_seconds() -> int:
    raw = os.environ.get("ATTACHMENT_RETENTION_INTERVAL_SECONDS")
    if raw and raw.isdigit():
        # Clamp between 1 minute (test-friendly) and 7 days.
        return max(60, min(int(raw), 7 * 86400))
    return DEFAULT_INTERVAL_SECONDS


# ---- Stats snapshot (mirrors dispatcher.SweepStats shape) ----


@dataclass
class RetentionStats:
    last_sweep_at: datetime | None = None
    last_sweep_duration_seconds: float | None = None
    last_sweep_purged: int = 0
    total_purged: int = 0
    total_iterations: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None
    running: bool = False
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    started_at: datetime | None = None


@dataclass
class _StatsHolder:
    stats: RetentionStats = field(default_factory=RetentionStats)
    lock: threading.Lock = field(default_factory=threading.Lock)


_STATS = _StatsHolder()


def get_retention_stats() -> RetentionStats:
    """Thread-safe snapshot of the retention sweep stats."""
    with _STATS.lock:
        s = _STATS.stats
        return RetentionStats(
            last_sweep_at=s.last_sweep_at,
            last_sweep_duration_seconds=s.last_sweep_duration_seconds,
            last_sweep_purged=s.last_sweep_purged,
            total_purged=s.total_purged,
            total_iterations=s.total_iterations,
            last_error=s.last_error,
            last_error_at=s.last_error_at,
            running=s.running,
            interval_seconds=s.interval_seconds,
            started_at=s.started_at,
        )


def _record_sweep(*, purged: int, duration_seconds: float, error: str | None) -> None:
    with _STATS.lock:
        s = _STATS.stats
        s.last_sweep_at = datetime.now(tz=timezone.utc)
        s.last_sweep_duration_seconds = duration_seconds
        s.last_sweep_purged = purged
        s.total_purged += purged
        s.total_iterations += 1
        if error is not None:
            s.last_error = error
            s.last_error_at = s.last_sweep_at


def _set_running(running: bool, *, interval_seconds: int | None = None) -> None:
    with _STATS.lock:
        s = _STATS.stats
        s.running = running
        if running and s.started_at is None:
            s.started_at = datetime.now(tz=timezone.utc)
        if not running:
            s.started_at = None
        if interval_seconds is not None:
            s.interval_seconds = interval_seconds


# ---- Per-attachment purge ----


async def _purge_one(
    storage: "StorageBase",
    file_backend: "StorageFileBackend",
    attachment: "ReferralAttachment",
    *,
    scope_organization_id: int | None,
    scope_user_id: int | None,
) -> bool:
    """Delete bucket object + DB row + emit audit.  Returns True iff the
    DB row was removed (bucket delete failures are logged, not blocking).
    """
    from docstats.scope import Scope

    scope = Scope(user_id=scope_user_id, organization_id=scope_organization_id)

    # Bucket first — an orphan bucket object is cheaper than an orphan DB
    # row (DB rows gate auth/audit; orphan bytes just waste Supabase
    # quota and get re-purged on the next sweep).  If the bucket call
    # fails, we still drop the DB row so the audit trail records the
    # policy decision — the orphan bytes are on the retention backlog.
    if attachment.storage_ref:
        try:
            await file_backend.delete(attachment.storage_ref)
        except StorageFileError:
            logger.exception(
                "retention: bucket delete failed for %s (leaving orphan)",
                attachment.storage_ref,
            )

    removed = storage.delete_referral_attachment(scope, attachment.referral_id, attachment.id)
    if not removed:
        logger.warning(
            "retention: DB delete returned False for attachment %s — possible race",
            attachment.id,
        )
        return False

    try:
        audit_record(
            storage,
            action="attachment.purged",
            request=None,
            actor_user_id=None,
            scope_user_id=scope_user_id,
            scope_organization_id=scope_organization_id,
            entity_type="attachment",
            entity_id=str(attachment.id),
            metadata={
                "referral_id": attachment.referral_id,
                "kind": attachment.kind,
                "storage_ref": attachment.storage_ref,
                "reason": "retention",
            },
        )
    except Exception:
        logger.exception("retention: audit_record failed for attachment %s", attachment.id)
    return True


# ---- Per-tenant sweep ----


async def _purge_tenant(
    storage: "StorageBase",
    file_backend: "StorageFileBackend",
    *,
    scope_organization_id: int | None,
    scope_user_id: int | None,
    retention_days: int,
    batch_size: int,
    max_batches: int,
) -> int:
    """Purge expired attachments for one tenant.  Returns count purged."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=retention_days)
    purged_total = 0
    for _ in range(max_batches):
        batch = storage.list_attachments_expired(
            cutoff,
            scope_organization_id=scope_organization_id,
            scope_user_id=scope_user_id,
            limit=batch_size,
        )
        if not batch:
            break
        for attachment in batch:
            ok = await _purge_one(
                storage,
                file_backend,
                attachment,
                scope_organization_id=scope_organization_id,
                scope_user_id=scope_user_id,
            )
            if ok:
                purged_total += 1
        # If the batch wasn't full, we're done even if max_batches remain.
        if len(batch) < batch_size:
            break
    return purged_total


# ---- Full sweep ----


async def run_sweep(
    storage: "StorageBase",
    file_backend: "StorageFileBackend",
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches_per_tenant: int = DEFAULT_MAX_BATCHES_PER_TENANT,
) -> int:
    """Run one retention sweep across every tenant.  Returns total purged.

    Exposed at module scope so tests can drive the sweep directly without
    standing up the full lifespan task.
    """
    total_purged = 0

    orgs = storage.list_all_organizations()
    for org in orgs:
        try:
            purged = await _purge_tenant(
                storage,
                file_backend,
                scope_organization_id=org.id,
                scope_user_id=None,
                retention_days=org.attachment_retention_days,
                batch_size=batch_size,
                max_batches=max_batches_per_tenant,
            )
            total_purged += purged
            if purged:
                logger.info("retention: org %s purged %d attachment(s)", org.id, purged)
        except Exception:
            logger.exception("retention: tenant sweep failed for org %s", org.id)

    solo_user_ids = storage.list_solo_user_ids_with_attachments()
    for uid in solo_user_ids:
        try:
            purged = await _purge_tenant(
                storage,
                file_backend,
                scope_organization_id=None,
                scope_user_id=uid,
                retention_days=DEFAULT_ATTACHMENT_RETENTION_DAYS,
                batch_size=batch_size,
                max_batches=max_batches_per_tenant,
            )
            total_purged += purged
            if purged:
                logger.info("retention: user %s purged %d attachment(s)", uid, purged)
        except Exception:
            logger.exception("retention: tenant sweep failed for user %s", uid)

    return total_purged


# ---- Lifespan loop ----


async def run(
    storage: "StorageBase",
    file_backend: "StorageFileBackend",
    *,
    stop_event: asyncio.Event,
    interval_seconds: int | None = None,
) -> None:
    """Lifespan coroutine — loops until ``stop_event`` is set."""
    interval = interval_seconds or _get_interval_seconds()
    _set_running(True, interval_seconds=interval)
    logger.info(
        "Attachment retention sweep started",
        extra={"interval_seconds": interval},
    )
    try:
        while not stop_event.is_set():
            loop = asyncio.get_event_loop()
            start = loop.time()
            purged = 0
            iteration_error: str | None = None
            try:
                purged = await run_sweep(storage, file_backend)
                if purged:
                    logger.info(
                        "Attachment retention sweep purged %d row(s)",
                        purged,
                        extra={"purged": purged},
                    )
            except Exception as exc:
                iteration_error = f"{type(exc).__name__}: {exc}"[:500]
                logger.exception("Attachment retention sweep raised")
            finally:
                _record_sweep(
                    purged=purged,
                    duration_seconds=max(0.0, loop.time() - start),
                    error=iteration_error,
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
    finally:
        _set_running(False)
    logger.info("Attachment retention sweep shutting down")
