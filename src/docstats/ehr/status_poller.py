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
    connection's encrypted access token via ``routes.ehr._maybe_refresh``.
    For Redox we mint a fresh JWT-bearer token at call time; the connection
    only provides ``iss`` (the ``{org}/{Environment}`` segment).

    Returns ``(None, None, "<reason>")`` on any unrecoverable error.
    """
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
        from docstats.routes.ehr import _maybe_refresh
    except Exception as exc:
        return None, None, f"{vendor}: refresh helper unavailable: {type(exc).__name__}"
    try:
        token = _maybe_refresh(conn, storage)
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
        try:
            storage.update_referral_ehr_status(
                referral.id,
                ehr_status=referral.ehr_status,  # leave prior status untouched
                polled_at=now,
                error=_truncate(err),
            )
        except Exception:
            logger.exception("Failed to record ehr_status auth error for referral %s", referral.id)
        return True, False

    try:
        snapshot = _vendor_read(
            referral.ehr_vendor,
            access_token=access_token or "",
            sr_id=referral.ehr_service_request_id,
            route=route,
        )
    except EHRError as exc:
        try:
            storage.update_referral_ehr_status(
                referral.id,
                ehr_status=referral.ehr_status,
                polled_at=now,
                error=_truncate(f"{type(exc).__name__}: {exc}"),
            )
        except Exception:
            logger.exception("Failed to record ehr_status fetch error for referral %s", referral.id)
        return True, False
    except ValueError as exc:
        # registry.get() raised — unknown vendor on this row.
        try:
            storage.update_referral_ehr_status(
                referral.id,
                ehr_status=referral.ehr_status,
                polled_at=now,
                error=_truncate(f"unknown vendor: {exc}"),
            )
        except Exception:
            logger.exception(
                "Failed to record ehr_status vendor error for referral %s", referral.id
            )
        return True, False
    except Exception:
        logger.exception("Unexpected error polling referral %s", referral.id)
        try:
            storage.update_referral_ehr_status(
                referral.id,
                ehr_status=referral.ehr_status,
                polled_at=now,
                error="unexpected",
            )
        except Exception:
            logger.exception("Failed to record ehr_status unexpected error %s", referral.id)
        return True, False

    new_status = snapshot.status
    prior_status = referral.ehr_status
    try:
        storage.update_referral_ehr_status(
            referral.id,
            ehr_status=new_status,
            polled_at=now,
            error=None,
        )
    except Exception:
        logger.exception("Failed to persist ehr_status for referral %s", referral.id)
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
    """One tick: fetch pollable rows, process each. Returns (processed, errors, changed)."""
    now = datetime.now(tz=timezone.utc)
    rows = storage.list_referrals_for_ehr_status_poll(
        limit=_get_batch_size(),
        max_age=_get_max_age(),
        now=now,
    )
    if not rows:
        return 0, 0, 0

    loop = asyncio.get_event_loop()

    async def _bounded(r):
        # Each vendor call is sync httpx — run in executor to free the event loop.
        return await loop.run_in_executor(None, _process_one, storage, r, now)

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
            loop = asyncio.get_event_loop()
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
