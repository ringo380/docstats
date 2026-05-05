"""Prior authorization (X12 278) domain model.

Represents a payer prior-auth submission against the Availity HIPAA Transactions
API.  FastAPI-free so tests can exercise it without the web stack.

The submission lifecycle:
    pending     — row created, request not yet sent
    submitted   — Availity returned an id; awaiting payer decision
    approved    — payer approved (reference_number set)
    denied      — payer denied (decision_reason set)
    cancelled   — request was cancelled (by us or payer)
    error       — fatal error during submit (4xx, malformed payload, etc.)
    unavailable — transient Availity outage; safe to retry
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Final

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------

PA_STATUS_VALUES: Final[tuple[str, ...]] = (
    "pending",
    "submitted",
    "approved",
    "denied",
    "cancelled",
    "error",
    "unavailable",
)

# Statuses where polling for a fresh decision still makes sense.
PA_STATUS_POLLABLE: Final[frozenset[str]] = frozenset({"submitted", "pending", "unavailable"})

# Terminal — polling won't change the answer.
PA_STATUS_TERMINAL: Final[frozenset[str]] = frozenset({"approved", "denied", "cancelled", "error"})

# Availity's response status strings → our internal status.
_AVAILITY_PA_STATUS_MAP: Final[dict[str, str]] = {
    "approved": "approved",
    "denied": "denied",
    "rejected": "denied",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "pending": "submitted",
    "in_review": "submitted",
    "in-review": "submitted",
    "submitted": "submitted",
}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class PriorAuthSubmission(BaseModel):
    """Storage record for one prior-auth submission attempt."""

    id: int | None = None

    # Scope (exactly one is set)
    scope_user_id: int | None = None
    scope_organization_id: int | None = None

    referral_id: int

    # Payer + member
    availity_payer_id: str
    payer_name: str | None = None
    member_id: str
    service_type: str

    # Clinical
    diagnosis_codes: list[str] = []
    procedure_codes: list[str] = []
    service_date: str | None = None  # ISO YYYY-MM-DD
    place_of_service: str | None = None

    # Lifecycle
    status: str  # PA_STATUS_VALUES
    availity_submission_id: str | None = None
    reference_number: str | None = None
    decision_date: datetime | None = None
    decision_reason: str | None = None
    error_message: str | None = None
    idempotency_key: str | None = None

    raw_request_json: str | None = None
    raw_response_json: str | None = None

    submitted_at: datetime | None = None
    last_polled_at: datetime | None = None
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_idempotency_key(
    *,
    referral_id: int,
    procedure_codes: list[str],
    service_date: str | None,
) -> str:
    """Deterministic key so retries collapse but a different request set creates a new row.

    Same (referral, sorted procedure codes, service date) → same key.
    """
    canonical = json.dumps(
        {
            "ref": referral_id,
            "proc": sorted(c.strip().upper() for c in procedure_codes if c.strip()),
            "date": (service_date or "").strip(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]
    return f"ref-{referral_id}-{digest}"


def parse_authorization_response(data: dict) -> dict:
    """Normalize an Availity authorization response into our column shape.

    Availity's REST 278 documents the following keys we care about:
        id                  — Availity submission id (string)
        status              — approved | denied | pending | in_review | cancelled
        referenceNumber     — payer-issued auth number
        decisionDate        — ISO timestamp
        decisionReason      — free-text reason on denial
        errors              — list of validation errors

    Returns a dict suitable for ``update_prior_auth_submission``.
    """
    raw_status = (data.get("status") or "").strip().lower()
    mapped = _AVAILITY_PA_STATUS_MAP.get(raw_status, "submitted")

    decision_dt: datetime | None = None
    raw_dt = data.get("decisionDate") or data.get("decision_date")
    if raw_dt:
        try:
            # Accept "2026-05-05T12:34:56Z" and "2026-05-05T12:34:56+00:00"
            normalized = str(raw_dt).replace("Z", "+00:00")
            decision_dt = datetime.fromisoformat(normalized)
        except (TypeError, ValueError):
            decision_dt = None

    return {
        "status": mapped,
        "availity_submission_id": str(data["id"]) if data.get("id") is not None else None,
        "reference_number": data.get("referenceNumber") or data.get("reference_number"),
        "decision_date": decision_dt,
        "decision_reason": data.get("decisionReason") or data.get("decision_reason"),
    }
