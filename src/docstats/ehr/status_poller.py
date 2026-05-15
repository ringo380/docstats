"""Issue #157 — background poller for remote ServiceRequest.status.

After ``_ehr_post_create_hook`` / ``_redox_post_create_hook`` POSTs a FHIR
ServiceRequest to a PCP's EHR (Phase 12.B), this lifespan task periodically
reads the remote resource and updates ``referrals.ehr_status`` so the patient
can see "Received by PCP" / "Completed by PCP" without logging into MyChart.

Shape mirrors ``docstats.delivery.dispatcher``:

- ``run(...)`` is the lifespan-managed loop, driven by ``stop_event``.
- ``_run_iteration(...)`` is one tick — extracted so tests can drive a single
  pass without standing up the full loop.
- ``EhrStatusPollStats`` is a thread-safe snapshot for the admin /health route.
- Configurable via ``EHR_STATUS_POLL_INTERVAL_SECONDS`` (default 600, clamp
  [60, 3600]), ``EHR_STATUS_POLL_BATCH_SIZE`` (default 50), and
  ``EHR_STATUS_POLL_MAX_AGE_DAYS`` (default 30 — older write-backs stop polling).
- Disabled under tests via ``DOCSTATS_SKIP_EHR_STATUS_POLLER=1`` (default in
  ``tests/conftest.py``).

Per-row work is wrapped in ``asyncio.shield`` so a SIGTERM mid-tick lets the
current vendor call finish before the outer cancel propagates. All exceptions
are logged and swallowed — one bad row never kills the poller.

The poller is scope-agnostic: it runs as a system actor, no Scope, no actor
user. ``record_referral_event`` is built with a Scope synthesized from the
row's denormalized columns.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from docstats.ehr import registry as _ehr_registry
from docstats.ehr.registry import EHRError

if TYPE_CHECKING:
    from docstats.domain.referrals import Referral
    from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)


DEFAULT_INTERVAL_SECONDS = 600  # 10 min
DEFAULT_BATCH_SIZE = 50
DEFAULT_MAX_AGE_DAYS = 30
_MAX_ERROR_LENGTH = 500

# Serializes ``_process_one`` storage writes when multiple poller workers run
# in parallel under the limiter. Production storage is supabase-py-over-HTTP
# (each call is an independent request, naturally thread-safe), so the lock
# contention is negligible there. Under local SQLite the shared
# ``sqlite3.Connection`` is NOT safe for concurrent use even with
# ``check_same_thread=False`` — this lock prevents the
# ``sqlite3.InterfaceError: bad parameter or other API misuse`` that
# concurrent ``with self._conn:`` blocks otherwise produce.
_STORAGE_WRITE_LOCK = threading.Lock()


def _get_interval_seconds() -> int:
    raw = os.environ.get("EHR_STATUS_POLL_INTERVAL_SECONDS")
    if raw and raw.lstrip("-").isdigit():
        return max(60, min(int(raw), 3600))
    return DEFAULT_INTERVAL_SECONDS


def _get_batch_size() -> int:
    raw = os.environ.get("EHR_STATUS_POLL_BATCH_SIZE")
    if raw and raw.isdigit():
        return max(1, min(int(raw), 500))
    return DEFAULT_BATCH_SIZE


def _get_max_age() -> timedelta:
    raw = os.environ.get("EHR_STATUS_POLL_MAX_AGE_DAYS")
    if raw and raw.isdigit():
        return timedelta(days=max(1, min(int(raw), 365)))
    return timedelta(days=DEFAULT_MAX_AGE_DAYS)


# ---- Health snapshot ------------------------------------------------------


@dataclass
class EhrStatusPollStats:
    last_sweep_at: datetime | None = None
    last_sweep_duration_seconds: float | None = None
    last_sweep_processed: int = 0
    last_sweep_errors: int = 0
    last_sweep_changed: int = 0
    total_iterations: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None
    running: bool = False
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    started_at: datetime | None = None


@dataclass
class _StatsHolder:
    stats: EhrStatusPollStats = field(default_factory=EhrStatusPollStats)
    lock: threading.Lock = field(default_factory=threading.Lock)


_STATS = _StatsHolder()


def get_poll_stats() -> EhrStatusPollStats:
    with _STATS.lock:
        s = _STATS.stats
        return EhrStatusPollStats(
            last_sweep_at=s.last_sweep_at,
            last_sweep_duration_seconds=s.last_sweep_duration_seconds,
            last_sweep_processed=s.last_sweep_processed,
            last_sweep_errors=s.last_sweep_errors,
            last_sweep_changed=s.last_sweep_changed,
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
    errors: int,
    changed: int,
    duration_seconds: float,
    error: str | None = None,
) -> None:
    with _STATS.lock:
        s = _STATS.stats
        s.last_sweep_at = datetime.now(tz=timezone.utc)
        s.last_sweep_duration_seconds = duration_seconds
        s.last_sweep_processed = processed
        s.last_sweep_errors = errors
        s.last_sweep_changed = changed
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


# ---- Per-row processing ---------------------------------------------------


def _truncate(s: str | None) -> str | None:
    if s is None:
        return None
    return s if len(s) <= _MAX_ERROR_LENGTH else s[: _MAX_ERROR_LENGTH - 1] + "…"


# Per-vendor ``_redact()`` already strips access/refresh/id-token fields from
# response bodies before raising ``EHRError``, but the raised string may still
# carry a stray ``Authorization: Bearer ...`` from a request-error message, or
# a FHIR URL with a patient id embedded in the path. ``_safe_excerpt`` is a
# defense-in-depth scrub before persisting to ``ehr_status_error`` (which is
# surfaced on ``/admin/deliveries/health.json``).
_BEARER_RE = re.compile(r"[Bb]earer\s+[A-Za-z0-9._\-]+", re.ASCII)


def _safe_excerpt(s: str | None) -> str | None:
    if s is None:
        return None
    scrubbed = _BEARER_RE.sub("Bearer [REDACTED]", s)
    return _truncate(scrubbed)


def _emit_status_change_event(
    storage: "StorageBase",
    referral: "Referral",
    *,
    from_status: str | None,
    to_status: str,
    vendor: str,
) -> None:
    """Append a referral_events row + audit log entry when the remote status changes.

    Scope is synthesized from the referral's denormalized scope columns —
    the poller has no user session. Both writes wrapped in their own
    try/except so a single failure doesn't propagate to the storage update.
    """
    from docstats.domain import audit as _audit_mod
    from docstats.scope import Scope

    scope = Scope(
        user_id=referral.scope_user_id,
        organization_id=referral.scope_organization_id,
        membership_role=None,
    )
    try:
        with _STORAGE_WRITE_LOCK:
            storage.record_referral_event(
                scope,
                referral.id,
                event_type="ehr_status",
                from_value=from_status,
                to_value=to_status,
                note=vendor,
                actor_user_id=None,
            )
    except Exception:
        logger.exception("Failed to emit ehr_status event for referral %s", referral.id)
    try:
        with _STORAGE_WRITE_LOCK:
            _audit_mod.record(
                storage,
                action="referral.ehr_status_changed",
                request=None,
                actor_user_id=None,
                scope_user_id=referral.scope_user_id,
                scope_organization_id=referral.scope_organization_id,
                entity_type="referral",
                entity_id=str(referral.id),
                metadata={"from": from_status, "to": to_status, "ehr_vendor": vendor},
            )
    except Exception:
        logger.exception("Failed to audit ehr_status change for referral %s", referral.id)


def _resolve_access_token(
    storage: "StorageBase",
    referral: "Referral",
) -> tuple[str | None, str | None, str | None]:
    """Return ``(access_token, iss_or_destination, error)`` for the row.

    For SMART vendors (epic/cerner/eclinicalworks) we use the stored
    connection's encrypted access token via ``ehr.tokens.maybe_refresh``.
    For Redox we mint a fresh JWT-bearer token at call time; the connection
    only provides ``iss`` (the ``{org}/{Environment}`` segment).

    Returns ``(None, None, "<reason>")`` on any unrecoverable error.
    """
    from docstats.ehr.tokens import maybe_refresh

    vendor = referral.ehr_vendor or ""
    conn_id = referral.ehr_connection_id

    if vendor == "redox":
        # Org-scoped JWT-bearer. Connection just supplies destination_path.
        if conn_id is None:
            return None, None, "redox: connection_id missing on referral"
        conn = storage.get_ehr_connection(conn_id)
        if conn is None or conn.revoked_at is not None:
            return None, None, "redox: connection revoked or missing"
        try:
            from docstats.ehr import redox as _redox
            from docstats.domain.ehr import REDOX_SCOPES

            token = _redox.request_access_token(scope=REDOX_SCOPES)
        except Exception as exc:  # RedoxConfigError or EHRError
            return None, None, f"redox token mint failed: {type(exc).__name__}"
        return token, conn.iss, None

    # SMART vendors: need a stored connection + maybe refresh the token.
    if conn_id is None:
        return None, None, f"{vendor}: connection_id missing on referral"
    conn = storage.get_ehr_connection(conn_id)
    if conn is None or conn.revoked_at is not None:
        return None, None, f"{vendor}: connection revoked or missing"
    try:
        token = maybe_refresh(conn, storage)
    except Exception as exc:
        return None, None, f"{vendor}: token refresh failed: {type(exc).__name__}"
    if not token:
        return None, None, f"{vendor}: no access token available"
    return token, conn.iss, None


def _vendor_read(vendor: str, *, access_token: str, sr_id: str, route: str | None):
    """Dispatch ``read_service_request`` to the right vendor module.

    SMART vendors take ``iss_override``; Redox takes ``destination_path``.
    """
    mod = _ehr_registry.get(vendor)
    if vendor == "redox":
        return mod.read_service_request(
            access_token=access_token,
            service_request_id=sr_id,
            destination_path=route,
        )
    return mod.read_service_request(
        access_token=access_token,
        service_request_id=sr_id,
        iss_override=route,
    )


def _persist_status(
    storage: "StorageBase",
    referral_id: int,
    *,
    ehr_status: str | None,
    polled_at: datetime,
    error: str | None,
) -> bool:
    """Serialized ehr_status write. Returns True on success."""
    try:
        with _STORAGE_WRITE_LOCK:
            storage.update_referral_ehr_status(
                referral_id,
                ehr_status=ehr_status,
                polled_at=polled_at,
                error=error,
            )
        return True
    except Exception:
        logger.exception("Failed to persist ehr_status for referral %s", referral_id)
        return False


def _process_one(storage: "StorageBase", referral: "Referral", now: datetime) -> tuple[bool, bool]:
    """Poll one referral. Returns ``(processed, changed)``.

    Never raises. Errors stash into ``ehr_status_error`` and bump
    ``ehr_status_polled_at`` so the row backs off naturally via the
    LRU ordering on the next tick.
    """
    if not referral.ehr_service_request_id or not referral.ehr_vendor:
        return False, False

    access_token, route, err = _resolve_access_token(storage, referral)
    if err is not None:
        # Leave prior ehr_status untouched on auth errors so the patient-facing
        # pill doesn't flicker to "unknown" during a transient token blip.
        _persist_status(
            storage,
            referral.id,
            ehr_status=referral.ehr_status,
            polled_at=now,
            error=_safe_excerpt(err),
        )
        return True, False

    try:
        snapshot = _vendor_read(
            referral.ehr_vendor,
            access_token=access_token or "",
            sr_id=referral.ehr_service_request_id,
            route=route,
        )
    except EHRError as exc:
        _persist_status(
            storage,
            referral.id,
            ehr_status=referral.ehr_status,
            polled_at=now,
            error=_safe_excerpt(f"{type(exc).__name__}: {exc}"),
        )
        return True, False
    except ValueError as exc:
        # registry.get() raised — unknown vendor on this row.
        _persist_status(
            storage,
            referral.id,
            ehr_status=referral.ehr_status,
            polled_at=now,
            error=_safe_excerpt(f"unknown vendor: {exc}"),
        )
        return True, False
    except Exception:
        logger.exception("Unexpected error polling referral %s", referral.id)
        _persist_status(
            storage,
            referral.id,
            ehr_status=referral.ehr_status,
            polled_at=now,
            error="unexpected",
        )
        return True, False

    new_status = snapshot.status
    prior_status = referral.ehr_status
    if not _persist_status(storage, referral.id, ehr_status=new_status, polled_at=now, error=None):
        return True, False

    changed = new_status != prior_status
    if changed:
        _emit_status_change_event(
            storage,
            referral,
            from_status=prior_status,
            to_status=new_status,
            vendor=referral.ehr_vendor or "unknown",
        )
    return True, changed


# ---- Sweep loop -----------------------------------------------------------


async def _run_iteration(storage: "StorageBase") -> tuple[int, int, int]:
    """One tick: fetch pollable rows, process each. Returns (processed, errors, changed).

    Concurrency is capped via ``async_limiter()`` (CLAUDE.md: "Batch paths use
    async_limiter() (not unbounded asyncio.gather)"). Without the cap a
    default batch of 50 would fire 50 simultaneous reads against the EHR and
    trip per-app rate limits.
    """
    from docstats.concurrency import async_limiter

    now = datetime.now(tz=timezone.utc)
    rows = storage.list_referrals_for_ehr_status_poll(
        limit=_get_batch_size(),
        max_age=_get_max_age(),
        now=now,
    )
    if not rows:
        return 0, 0, 0

    loop = asyncio.get_running_loop()
    limiter = async_limiter()

    async def _bounded(r):
        # Each vendor call is sync httpx — run in executor to free the event
        # loop. The limiter caps the concurrent in-flight executor work; under
        # SQLite (local dev / tests) ``_STORAGE_WRITE_LOCK`` also serializes
        # the few storage writes inside ``_process_one`` so the shared
        # ``sqlite3.Connection`` object isn't hit concurrently.
        async with limiter:
            return await loop.run_in_executor(None, _process_one, storage, r, now)

    # ``asyncio.shield`` lets the in-flight tasks finish if the outer ``run``
    # coroutine is cancelled mid-tick. It does NOT interrupt the executor
    # thread — that's a side benefit of the executor model itself (Python
    # can't cancel a running OS thread). What shield buys us: the asyncio
    # task awaiting the executor result still gets to write the result back.
    results = await asyncio.gather(
        *(asyncio.shield(_bounded(r)) for r in rows),
        return_exceptions=True,
    )

    processed = 0
    errors = 0
    changed = 0
    for r in results:
        if isinstance(r, BaseException):
            errors += 1
            continue
        ok, did_change = r
        if ok:
            processed += 1
            if did_change:
                changed += 1
    return processed, errors, changed


async def run(
    storage: "StorageBase",
    *,
    stop_event: asyncio.Event,
    interval_seconds: int | None = None,
) -> None:
    """Lifespan loop. Never raises; all exceptions logged + swallowed."""
    interval = interval_seconds or _get_interval_seconds()
    _set_running(True, interval_seconds=interval)
    logger.info("EHR status poller started", extra={"interval_seconds": interval})
    try:
        while not stop_event.is_set():
            loop = asyncio.get_running_loop()
            start = loop.time()
            processed = errors = changed = 0
            iteration_error: str | None = None
            try:
                processed, errors, changed = await _run_iteration(storage)
                if processed:
                    logger.info(
                        "EHR status poller processed %d row(s) (changed=%d, errors=%d)",
                        processed,
                        changed,
                        errors,
                    )
            except Exception as exc:
                iteration_error = f"{type(exc).__name__}: {exc}"[:500]
                logger.exception("EHR status poller iteration raised")
            finally:
                _record_sweep(
                    processed=processed,
                    errors=errors,
                    changed=changed,
                    duration_seconds=max(0.0, loop.time() - start),
                    error=iteration_error,
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
    finally:
        _set_running(False)
    logger.info("EHR status poller shutting down")
