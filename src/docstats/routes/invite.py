"""Invitation redemption flow — Phase 6.F.

This is the ONE admin-adjacent surface that's not gated on
``require_admin_scope`` — the whole point is for a non-member (possibly a
brand-new user) to click a magic link and join an org. Guards here:

- The token must exist and be pending (not accepted, not revoked, not
  expired).
- The caller must be logged in.
- The logged-in user's email must match the invitation email. Anyone
  else sees a friendly "this invitation was for <email>, please log in
  as that user" message rather than silently becoming a member.

After a successful accept the user's ``active_org_id`` is pointed at the
org, so the next page load reflects the new membership.

Routes:
- ``GET /invite/{token}`` — landing page. Shows invitation details +
  sign-in / sign-up prompt if anonymous, or an Accept button if the
  email matches.
- ``POST /invite/{token}/accept`` — redeem. Creates a membership,
  marks the invitation accepted, audits ``admin.member.joined``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, Response

from docstats.auth import get_current_user, require_user
from docstats.domain.audit import record as audit_record
from docstats.domain.invitations import Invitation
from docstats.routes._common import render, saved_count
from docstats.storage import get_storage
from docstats.storage_base import StorageBase, normalize_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invite", tags=["invite"])

# Status strings rendered on the landing page when the invitation isn't
# redeemable. Module-level constants so template tests can pin them.
STATUS_PENDING = "pending"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"
STATUS_ACCEPTED = "accepted"


def _classify(invitation: Invitation, now: datetime | None = None) -> str:
    """Return a string describing the invitation's current state."""
    if invitation.accepted_at is not None:
        return STATUS_ACCEPTED
    if invitation.revoked_at is not None:
        return STATUS_REVOKED
    if now is None:
        now = datetime.now(tz=timezone.utc)
    expires = invitation.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= now:
        return STATUS_EXPIRED
    return STATUS_PENDING


def _load_invitation(storage: StorageBase, token: str) -> Invitation:
    """Fetch the invitation or 404. Expired / revoked / accepted rows are
    still loaded — the landing page renders a status-specific message
    rather than pretending the token never existed."""
    invitation = storage.get_invitation_by_token(token)
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation not found.")
    return invitation


def _ctx(
    request: Request,
    user: dict | None,
    storage: StorageBase,
    invitation: Invitation,
    *,
    status: str,
    email_matches: bool,
    org_name: str,
    errors: list[str] | None = None,
) -> dict:
    """Template context for the redemption landing page."""
    return {
        "request": request,
        "active_page": "invite",
        "user": user,
        "saved_count": saved_count(storage, user["id"]) if user else 0,
        "invitation": invitation,
        "org_name": org_name,
        "status": status,
        "email_matches": email_matches,
        "errors": errors or [],
    }


@router.get("/{token}", response_class=HTMLResponse)
async def invite_landing(
    request: Request,
    token: str = Path(..., min_length=8, max_length=128),
    current_user: dict | None = Depends(get_current_user),
    storage: StorageBase = Depends(get_storage),
):
    """Render the invitation landing page.

    Anonymous visitors see "please log in as <email> to accept". Logged-in
    visitors whose email matches see an Accept button. Logged-in visitors
    whose email doesn't match see a "this isn't your invitation" message.
    """
    invitation = _load_invitation(storage, token)
    status = _classify(invitation)
    org = storage.get_organization(invitation.organization_id)
    org_name = org.name if org is not None else "(unknown)"

    email_matches = False
    if current_user is not None:
        email_matches = normalize_email(current_user.get("email", "")) == invitation.email

    return render(
        "invite_accept.html",
        _ctx(
            request,
            current_user,
            storage,
            invitation,
            status=status,
            email_matches=email_matches,
            org_name=org_name,
        ),
    )


@router.post("/{token}/accept", response_class=HTMLResponse)
async def invite_accept(
    request: Request,
    token: str = Path(..., min_length=8, max_length=128),
    current_user: dict = Depends(require_user),
    storage: StorageBase = Depends(get_storage),
):
    """Redeem the invitation: create (or re-activate) membership, mark
    accepted, point active_org_id at the new org.

    The storage-side ``mark_invitation_accepted`` is atomic against
    revoke/expire checks, but we still do belt-and-suspenders validation
    up-front so the 422 / 410 status codes the client sees match the
    actual reason.
    """
    invitation = _load_invitation(storage, token)
    status = _classify(invitation)
    org = storage.get_organization(invitation.organization_id)
    org_name = org.name if org is not None else "(unknown)"

    email_matches = normalize_email(current_user.get("email", "")) == invitation.email

    if status != STATUS_PENDING:
        # Re-render the landing page with the status-specific message;
        # 410 Gone is the semantically closest status for expired /
        # revoked, but rendering HTML is more useful than a JSON error
        # to a browser client.
        return render(
            "invite_accept.html",
            _ctx(
                request,
                current_user,
                storage,
                invitation,
                status=status,
                email_matches=email_matches,
                org_name=org_name,
                errors=[f"This invitation is {status}."],
            ),
        )

    if not email_matches:
        return render(
            "invite_accept.html",
            _ctx(
                request,
                current_user,
                storage,
                invitation,
                status=status,
                email_matches=False,
                org_name=org_name,
                errors=[
                    f"This invitation was sent to {invitation.email}. "
                    "Please sign in as that user to accept."
                ],
            ),
        )

    # Create (or reactivate) the membership. ``create_membership`` is
    # upsert-style — it'll reactivate a soft-deleted row or flip a
    # stale role in the same table (see storage_base docstrings).
    storage.create_membership(
        organization_id=invitation.organization_id,
        user_id=current_user["id"],
        role=invitation.role,
        invited_by_user_id=invitation.invited_by_user_id,
    )

    # Mark the invitation accepted. If this returns False the row
    # slipped state (revoked / expired between landing and accept) —
    # rare, but the membership is already written so we keep it
    # (admin can revert manually). We still audit the attempt.
    accepted_ok = storage.mark_invitation_accepted(invitation.id)
    if not accepted_ok:
        logger.warning(
            "Invitation %s accepted AFTER storage state change; "
            "membership created but accept-mark failed",
            invitation.id,
        )

    # Point the user's active_org_id at the new org so the next page
    # load reflects the membership. Don't clobber an existing active
    # org — the user may want to keep their current one and switch
    # manually (future org-switcher UI, Phase 7).
    if current_user.get("active_org_id") is None:
        try:
            storage.set_active_org(current_user["id"], invitation.organization_id)
        except Exception:
            logger.exception(
                "Failed to set active_org_id for user %s after accept",
                current_user["id"],
            )

    audit_record(
        storage,
        action="admin.member.joined",
        request=request,
        actor_user_id=current_user["id"],
        scope_organization_id=invitation.organization_id,
        entity_type="membership",
        entity_id=str(current_user["id"]),
        metadata={"role": invitation.role, "invitation_id": invitation.id},
    )

    # Land the user on the admin overview if they can see it (admin+),
    # else the referrals workspace (default authenticated home).
    if current_user.get("is_org_admin"):
        dest = "/admin"
    else:
        dest = "/referrals"
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": dest})
    return Response(status_code=303, headers={"Location": dest})
