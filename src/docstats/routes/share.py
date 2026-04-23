"""Share-token viewer routes — Phase 9.B.

These routes are PUBLIC (no ``require_user`` dependency). An external
recipient — a specialist's office, a patient — lands here via the link
embedded in a referral email. PHI is gated behind a 2FA step.

Routes
------
GET  /share/{token}          — landing / gate page
POST /share/{token}/verify   — 2FA check; redirects to /view on success
GET  /share/{token}/view     — read-only referral viewer (session-gated)

Security design
---------------
- Token plaintext is never stored; only SHA-256(plaintext) in the DB.
- 2FA (patient DOB or last-4 phone) must be proved before PHI renders.
- Rate-limit: 10 attempts per IP per 15 minutes on the verify endpoint.
- Per-token brute-force guard: 5 wrong answers revokes the token.
- Every successful view emits audit row ``share.view`` with IP + UA.
- Session cookie (``share_verified_{token_id}``) gates the view route
  within the same browser session — no re-prompting after a successful
  2FA until the session expires.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware  # noqa: F401 — see web.py

from docstats.domain.audit import record as audit_record
from docstats.domain.share_tokens import MAX_FAILED_ATTEMPTS, hash_token, verify_second_factor
from docstats.routes._common import render
from docstats.routes._rate_limit import RateLimiter
from docstats.storage import get_storage
from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/share", tags=["share"])

# 10 verify attempts per IP per 15 minutes
_rate_limiter = RateLimiter(max_attempts=10, window_seconds=900)

_SESSION_KEY_PREFIX = "share_verified_"


def _session_key(token_id: int) -> str:
    return f"{_SESSION_KEY_PREFIX}{token_id}"


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _load_token(token: str, storage: StorageBase):
    tok_hash = hash_token(token)
    return storage.get_share_token_by_hash(tok_hash)


def _ctx(request: Request, extra: dict) -> dict:
    return {"request": request, "user": None, **extra}


@router.get("/{token}")
async def share_landing(
    request: Request,
    token: str,
    storage: StorageBase = Depends(get_storage),
) -> Response:
    share_token = _load_token(token, storage)
    if share_token is None:
        raise HTTPException(status_code=404, detail="Link not found.")

    if not share_token.is_valid:
        return HTMLResponse(
            render(
                "share_invalid.html",
                _ctx(request, {"reason": "revoked" if share_token.revoked_at else "expired"}),
            )
        )

    # Already verified in this session → redirect straight to view
    if request.session.get(_session_key(share_token.id)):
        return RedirectResponse(f"/share/{token}/view", status_code=302)

    if not share_token.requires_second_factor:
        # No 2FA required — mark verified and redirect
        request.session[_session_key(share_token.id)] = True
        return RedirectResponse(f"/share/{token}/view", status_code=302)

    return HTMLResponse(
        render(
            "share_gate.html",
            _ctx(
                request,
                {
                    "token": token,
                    "second_factor_kind": share_token.second_factor_kind,
                    "error": None,
                },
            ),
        )
    )


@router.post("/{token}/verify")
async def share_verify(
    request: Request,
    token: str,
    answer: str = Form(..., max_length=32),
    storage: StorageBase = Depends(get_storage),
) -> Response:
    ip = _client_ip(request)
    if not _rate_limiter.allow(ip):
        raise HTTPException(status_code=429, detail="Too many attempts — try again later.")
    share_token = _load_token(token, storage)
    if share_token is None or not share_token.is_valid:
        raise HTTPException(status_code=404, detail="Link not found or expired.")

    if not share_token.requires_second_factor or share_token.second_factor_hash is None:
        request.session[_session_key(share_token.id)] = True
        return RedirectResponse(f"/share/{token}/view", status_code=303)

    correct = verify_second_factor(answer, share_token.second_factor_hash)
    if not correct:
        storage.increment_share_token_failures(share_token.id)
        # Re-fetch to get the updated count
        updated = storage.get_share_token_by_hash(hash_token(token))
        if updated and updated.failed_attempts >= MAX_FAILED_ATTEMPTS:
            storage.revoke_share_token(share_token.id)
            logger.warning(
                "Share token %s revoked after %d failed 2FA attempts from IP %s",
                share_token.id,
                updated.failed_attempts,
                ip,
            )
            return HTMLResponse(
                render(
                    "share_invalid.html",
                    _ctx(request, {"reason": "revoked"}),
                )
            )
        return HTMLResponse(
            render(
                "share_gate.html",
                _ctx(
                    request,
                    {
                        "token": token,
                        "second_factor_kind": share_token.second_factor_kind,
                        "error": "Incorrect answer — please try again.",
                    },
                ),
            )
        )

    request.session[_session_key(share_token.id)] = True
    return RedirectResponse(f"/share/{token}/view", status_code=303)


@router.get("/{token}/view")
async def share_view(
    request: Request,
    token: str,
    storage: StorageBase = Depends(get_storage),
) -> Response:
    share_token = _load_token(token, storage)
    if share_token is None or not share_token.is_valid:
        raise HTTPException(status_code=404, detail="Link not found or expired.")

    if not request.session.get(_session_key(share_token.id)):
        return RedirectResponse(f"/share/{token}", status_code=302)

    # Fetch the delivery and referral — no user scope needed (public route)
    delivery = storage.get_delivery(None, share_token.delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="Link no longer valid.")

    referral = storage.get_referral(None, delivery.referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found.")

    patient = storage.get_patient(None, referral.patient_id) if referral.patient_id else None
    diagnoses = storage.list_referral_diagnoses(None, referral.id)
    medications = storage.list_referral_medications(None, referral.id)
    allergies = storage.list_referral_allergies(None, referral.id)
    attachments = storage.list_referral_attachments(None, referral.id)

    storage.increment_share_token_views(share_token.id)

    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")[:500]
    audit_record(
        storage,
        action="share.view",
        request=request,
        metadata={
            "share_token_id": share_token.id,
            "delivery_id": share_token.delivery_id,
            "referral_id": referral.id,
            "ip": ip,
            "ua": ua,
        },
    )

    return HTMLResponse(
        render(
            "referral_share.html",
            _ctx(
                request,
                {
                    "referral": referral,
                    "patient": patient,
                    "diagnoses": diagnoses,
                    "medications": medications,
                    "allergies": allergies,
                    "attachments": attachments,
                    "share_token": share_token,
                },
            ),
        )
    )
