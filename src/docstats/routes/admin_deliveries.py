"""Admin delivery console — Phase 9.E.

Operator-grade visibility + control for outbound deliveries.  Every route
here is gated by ``require_admin_scope`` (same contract as Phase 6.B–6.F):
solo users and sub-admin org members get 403.

Routes
------
GET  /admin/deliveries                    — filterable list + pagination
GET  /admin/deliveries/health             — queue + sweeper snapshot (HTML)
GET  /admin/deliveries/health.json        — same snapshot as JSON
GET  /admin/deliveries/{id}               — detail + attempt history
POST /admin/deliveries/{id}/cancel        — admin cancel (idempotent)

Design notes
------------
- Filter vocabulary mirrors the Phase 9.A delivery model: channel, status,
  referral_id, since/until (``created_at`` window).  ``since`` is inclusive,
  ``until`` is exclusive — matches the audit log filter contract from 6.E.
- Pagination uses the same offset-based ``page_size + 1`` pattern as
  ``/admin/audit`` — no count query, straightforward has-next detection.
- Cancel audit action is ``admin.delivery.cancel`` so the admin trail reads
  distinctly from the coordinator-initiated ``delivery.cancel``.
- Health snapshot joins two data sources:
    1. ``storage.get_delivery_queue_stats(...)`` — DB-backed row counts +
       oldest-queued age.
    2. ``dispatcher.get_sweep_stats()`` — process-local last-sweep info.
  Multi-worker deploys see only the worker that served the request for
  the dispatcher stats; the DB counts are authoritative.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from docstats.auth import require_user
from docstats.delivery.dispatcher import get_sweep_stats
from docstats.domain.audit import record as audit_record
from docstats.domain.deliveries import CHANNEL_VALUES, DELIVERY_STATUS_VALUES
from docstats.domain.orgs import Organization
from docstats.routes._common import render, saved_count
from docstats.routes.admin import require_admin_scope
from docstats.scope import Scope
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/deliveries", tags=["admin-deliveries"])

_PAGE_SIZE = 50
_MAX_OFFSET = 10_000


def _require_org(scope: Scope, storage: StorageBase) -> Organization:
    assert scope.organization_id is not None
    org = storage.get_organization(scope.organization_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return org


def _ctx(
    request: Request,
    user: dict,
    storage: StorageBase,
    scope: Scope,
    org: Organization,
    **extra: object,
) -> dict:
    return {
        "request": request,
        "active_page": "admin",
        "active_section": "deliveries",
        "user": user,
        "saved_count": saved_count(storage, user["id"]),
        "scope": scope,
        "org": org,
        **extra,
    }


def _parse_date_filter(value: str | None, *, end_of_day: bool) -> datetime | None:
    """Parse ``YYYY-MM-DD`` → UTC.  ``end_of_day=True`` advances by 1 day so
    the admin's date range is inclusive on both ends (storage treats ``until``
    as exclusive).  Mirrors ``admin.audit._parse_date_filter``."""
    if value is None or not value.strip():
        return None
    try:
        d = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Date must be YYYY-MM-DD: {value!r}")
    if end_of_day:
        d = d + timedelta(days=1)
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def _parse_optional_int(value: str | None, *, name: str, min_value: int = 1) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value.strip())
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} must be an integer: {value!r}")
    if parsed < min_value:
        raise HTTPException(status_code=422, detail=f"{name} must be >= {min_value}")
    return parsed


def _normalize_enum(value: str | None, allowed: tuple[str, ...], *, name: str) -> str | None:
    if value is None or not value.strip():
        return None
    v = value.strip()
    if v not in allowed:
        raise HTTPException(status_code=422, detail=f"{name} must be one of {list(allowed)}: {v!r}")
    return v


@router.get("", response_class=HTMLResponse)
async def deliveries_list(
    request: Request,
    channel: str | None = Query(None, max_length=16),
    status: str | None = Query(None, max_length=16),
    referral_id: str | None = Query(None, max_length=16),
    since: str | None = Query(None, max_length=10),
    until: str | None = Query(None, max_length=10),
    offset: int = Query(0, ge=0, le=_MAX_OFFSET),
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    org = _require_org(scope, storage)

    channel_clean = _normalize_enum(channel, CHANNEL_VALUES, name="channel")
    status_clean = _normalize_enum(status, DELIVERY_STATUS_VALUES, name="status")
    referral_id_int = _parse_optional_int(referral_id, name="referral_id")
    since_dt = _parse_date_filter(since, end_of_day=False)
    until_dt = _parse_date_filter(until, end_of_day=True)

    rows = storage.list_deliveries_for_admin(
        scope_organization_id=scope.organization_id,
        channel=channel_clean,
        status=status_clean,
        referral_id=referral_id_int,
        since=since_dt,
        until=until_dt,
        limit=_PAGE_SIZE + 1,
        offset=offset,
    )
    has_next = len(rows) > _PAGE_SIZE
    if has_next:
        rows = rows[:_PAGE_SIZE]
    has_prev = offset > 0
    raw_next = offset + _PAGE_SIZE
    next_offset = raw_next if has_next and raw_next <= _MAX_OFFSET else None
    prev_offset = max(0, offset - _PAGE_SIZE) if has_prev else None

    filters = {
        "channel": channel_clean or "",
        "status": status_clean or "",
        "referral_id": referral_id or "",
        "since": since or "",
        "until": until or "",
    }
    return render(
        "admin/deliveries_list.html",
        _ctx(
            request,
            current_user,
            storage,
            scope,
            org,
            deliveries=rows,
            filters=filters,
            channel_values=CHANNEL_VALUES,
            status_values=DELIVERY_STATUS_VALUES,
            page_size=_PAGE_SIZE,
            offset=offset,
            next_offset=next_offset,
            prev_offset=prev_offset,
        ),
    )


@router.get("/health", response_class=HTMLResponse)
async def deliveries_health(
    request: Request,
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    org = _require_org(scope, storage)
    stats = storage.get_delivery_queue_stats(scope_organization_id=scope.organization_id)
    sweep = get_sweep_stats()
    return render(
        "admin/deliveries_health.html",
        _ctx(
            request,
            current_user,
            storage,
            scope,
            org,
            queue_stats=stats,
            sweep_stats=sweep,
        ),
    )


@router.get("/health.json")
async def deliveries_health_json(
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
) -> JSONResponse:
    stats = storage.get_delivery_queue_stats(scope_organization_id=scope.organization_id)
    sweep = get_sweep_stats()
    return JSONResponse(
        {
            "queue": stats.model_dump(),
            "sweeper": {
                "running": sweep.running,
                "interval_seconds": sweep.interval_seconds,
                "total_iterations": sweep.total_iterations,
                "last_sweep_at": sweep.last_sweep_at.isoformat() if sweep.last_sweep_at else None,
                "last_sweep_duration_seconds": sweep.last_sweep_duration_seconds,
                "last_sweep_processed": sweep.last_sweep_processed,
                "last_error": sweep.last_error,
                "last_error_at": sweep.last_error_at.isoformat() if sweep.last_error_at else None,
                "started_at": sweep.started_at.isoformat() if sweep.started_at else None,
            },
        }
    )


@router.get("/{delivery_id}", response_class=HTMLResponse)
async def delivery_detail(
    request: Request,
    delivery_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    org = _require_org(scope, storage)
    delivery = storage.get_delivery(scope, delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="Delivery not found.")
    attempts = storage.list_delivery_attempts(scope, delivery_id)
    referral = storage.get_referral(scope, delivery.referral_id)
    patient = None
    if referral is not None and referral.patient_id:
        patient = storage.get_patient(scope, referral.patient_id)

    return render(
        "admin/delivery_detail.html",
        _ctx(
            request,
            current_user,
            storage,
            scope,
            org,
            delivery=delivery,
            attempts=attempts,
            referral=referral,
            patient=patient,
        ),
    )


@router.post("/{delivery_id}/cancel")
async def delivery_cancel(
    request: Request,
    delivery_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_user),
    scope: Scope = Depends(require_admin_scope),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    delivery = storage.get_delivery(scope, delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="Delivery not found.")

    cancelled = storage.cancel_delivery(scope, delivery_id, cancelled_by_user_id=current_user["id"])
    if cancelled:
        audit_record(
            storage,
            action="admin.delivery.cancel",
            request=request,
            actor_user_id=current_user["id"],
            scope_organization_id=scope.organization_id,
            entity_type="delivery",
            entity_id=str(delivery_id),
            metadata={
                "referral_id": delivery.referral_id,
                "channel": delivery.channel,
                "previous_status": delivery.status,
            },
        )

    # Match the admin-module redirect discipline: htmx gets HX-Redirect,
    # plain forms get 303.  Full-page-nav after cancel lands back on the
    # delivery detail so the admin sees the cancelled state immediately.
    dest = f"/admin/deliveries/{delivery_id}"
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": dest})
    return Response(status_code=303, headers={"Location": dest})
