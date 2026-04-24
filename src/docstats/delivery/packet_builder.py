"""Delivery-side packet rendering — Phase 10.D.

The lifespan dispatcher (Phase 9.A) invokes ``Channel.send(delivery,
packet_bytes)`` on every successful dispatch; the bytes come from this
module.  Contract matches the user-initiated export route in
``routes/exports.py`` — both paths share the ``fetch_attachment_pdfs``
helper and the ``render_packet`` concatenator so a fax and a downloaded
PDF look identical.

Delivery scope
--------------
A ``Delivery`` row carries ``scope_user_id`` / ``scope_organization_id``
denormalized from its parent referral.  We rebuild a :class:`Scope` from
those columns and pass it through to the export path; the dispatcher
itself has no user session.

packet_artifact contract
------------------------
``delivery.packet_artifact`` is a JSON dict persisted at enqueue time.
Recognized keys:

- ``include`` (list[str]): ordered artifact names to render.  Unknown or
  ``packet``-nested tokens are silently dropped (the enqueue validator
  in ``routes/delivery.py`` catches most of these; we defense-in-depth
  here).  Falls back to ``_DEFAULT_PACKET_INCLUDE`` when absent.

Email-specific keys (``share_token_url``, ``email_subject``, ``org_name``)
are consumed by the email channel, not by this module.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from docstats.exports import (
    ARTIFACT_ATTACHMENT_PDFS,
    ARTIFACT_ATTACHMENTS_CHECKLIST,
    ARTIFACT_FAX_COVER,
    ARTIFACT_PACKET,
    ARTIFACT_REFERRAL_SUMMARY,
    fetch_attachment_pdfs,
    render_attachments_checklist,
    render_fax_cover,
    render_missing_info,
    render_packet,
    render_patient_summary,
    render_referral_summary,
    render_scheduling_summary,
)
from docstats.scope import Scope

if TYPE_CHECKING:
    from docstats.domain.deliveries import Delivery
    from docstats.storage_base import StorageBase
    from docstats.storage_files.base import StorageFileBackend

logger = logging.getLogger(__name__)

# Default packet content when the caller didn't pin ``include`` in the
# packet_artifact.  Matches ``routes/exports._DEFAULT_PACKET_INCLUDE`` —
# keep in sync so both paths produce identical output.
_DEFAULT_INCLUDE: tuple[str, ...] = (
    ARTIFACT_FAX_COVER,
    ARTIFACT_REFERRAL_SUMMARY,
    ARTIFACT_ATTACHMENTS_CHECKLIST,
)

# Map artifact name → sync renderer fn.  ``attachment_pdfs`` is handled
# specially below (async fetch + byte splice); every other artifact
# renders synchronously given its extra kwargs.
_RENDERERS: dict[str, object] = {
    "summary": render_referral_summary,
    "scheduling": render_scheduling_summary,
    "patient": render_patient_summary,
    "attachments": render_attachments_checklist,
    "missing_info": render_missing_info,
    "fax_cover": render_fax_cover,
}


def _build_scope(delivery: "Delivery") -> Scope:
    return Scope(
        user_id=delivery.scope_user_id,
        organization_id=delivery.scope_organization_id,
        membership_role=None,
    )


def _fetch_extra(
    artifact: str,
    *,
    storage: "StorageBase",
    scope: Scope,
    referral,
    patient,
) -> dict:
    """Per-artifact extra kwargs mirror ``routes/exports._fetch_*``.

    Kept as a small dispatch here (rather than re-importing the route
    module's private fetchers) so this module stays route-free.
    """
    if artifact == "summary":
        return {
            "diagnoses": storage.list_referral_diagnoses(scope, referral.id),
            "medications": storage.list_referral_medications(scope, referral.id),
            "allergies": storage.list_referral_allergies(scope, referral.id),
            "attachments": storage.list_referral_attachments(scope, referral.id),
        }
    if artifact == "attachments":
        return {
            "attachments": storage.list_referral_attachments(scope, referral.id),
        }
    if artifact == "missing_info":
        from docstats.domain.rules import rules_based_completeness

        return {
            "completeness": rules_based_completeness(storage, scope, referral),
        }
    return {}


def _parse_include(raw) -> list[str]:
    """Normalize ``packet_artifact.include`` into a list of valid tokens."""
    if not isinstance(raw, list):
        return list(_DEFAULT_INCLUDE)
    out: list[str] = []
    seen: set[str] = set()
    for tok in raw:
        if not isinstance(tok, str):
            continue
        if tok == ARTIFACT_PACKET:
            continue  # nested packet not supported
        if tok != ARTIFACT_ATTACHMENT_PDFS and tok not in _RENDERERS:
            continue  # unknown artifact — dispatcher is defense-in-depth
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out or list(_DEFAULT_INCLUDE)


async def build_delivery_packet(
    storage: "StorageBase",
    file_backend: "StorageFileBackend",
    delivery: "Delivery",
) -> bytes:
    """Return the concatenated packet for ``delivery``.

    Raises ``ValueError`` if the referral or patient row is missing (the
    dispatcher treats this as a fatal ``DeliveryError`` — retrying won't
    help if the upstream record vanished).  File-backend errors on
    individual attachments are logged and skipped by
    ``fetch_attachment_pdfs``; a missing blob never fails the whole
    packet.
    """
    scope = _build_scope(delivery)
    referral = storage.get_referral(scope, delivery.referral_id)
    if referral is None:
        raise ValueError(f"Referral {delivery.referral_id} not found in scope")
    patient = storage.get_patient(scope, referral.patient_id) if referral.patient_id else None
    if patient is None:
        # Packet renderers universally require a Patient row; fail
        # explicitly rather than producing a PDF with placeholder text.
        raise ValueError(f"Patient for referral {delivery.referral_id} unavailable")

    raw_artifact = delivery.packet_artifact or {}
    parts_order = _parse_include(raw_artifact.get("include"))

    generated_at = datetime.now(tz=timezone.utc)
    generated_by_label = "Delivery dispatcher"
    loop = asyncio.get_running_loop()

    # Phase 10.D — fetch attachment PDF bytes once up front (async),
    # then splice them in during the synchronous render loop.
    attachment_pdfs: list[bytes] = []
    if ARTIFACT_ATTACHMENT_PDFS in parts_order:
        attachment_pdfs = [
            data
            for _aid, data in await fetch_attachment_pdfs(
                storage=storage,
                scope=scope,
                referral=referral,
                file_backend=file_backend,
            )
        ]

    parts: list[bytes] = []
    for name in parts_order:
        if name == ARTIFACT_ATTACHMENT_PDFS:
            parts.extend(attachment_pdfs)
            continue
        renderer = _RENDERERS.get(name)
        if renderer is None:
            continue
        extra = _fetch_extra(name, storage=storage, scope=scope, referral=referral, patient=patient)
        try:
            part = await loop.run_in_executor(
                None,
                lambda r=renderer, ex=extra: r(  # type: ignore[misc]
                    referral=referral,
                    patient=patient,
                    generated_at=generated_at,
                    generated_by_label=generated_by_label,
                    **ex,
                ),
            )
        except Exception:
            logger.exception(
                "Dispatcher packet: render failed for artifact %s on delivery %s",
                name,
                delivery.id,
            )
            continue
        parts.append(part)

    if not parts:
        raise ValueError(
            f"Delivery {delivery.id} packet is empty — include list resolved to 0 parts"
        )

    return await loop.run_in_executor(
        None,
        lambda: render_packet(
            referral=referral,
            patient=patient,
            parts=parts,
            generated_at=generated_at,
            generated_by_label=generated_by_label,
        ),
    )
