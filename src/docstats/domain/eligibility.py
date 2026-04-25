"""Eligibility check domain model.

Represents a real-time insurance eligibility inquiry (X12 270/271) run via
the Availity Healthcare HIPAA Transactions API.  The domain module is
FastAPI-free so tests can exercise it without bringing up the web stack.

Parsed results carry the fields most relevant to referral decisions:
active coverage, referral requirement, prior-auth requirement, copay, and
deductible status.  Raw response JSON is stored alongside for audit/debug.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel

if TYPE_CHECKING:
    from docstats.domain.referrals import CompletenessItem  # noqa: F401 — used in overlay_eligibility
    from docstats.domain.rules import CompletenessReportV2

# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------

ELIGIBILITY_CHECK_STATUS_VALUES: Final[tuple[str, ...]] = (
    "pending",  # enqueued, not yet sent to clearinghouse
    "complete",  # 271 received and parsed
    "error",  # clearinghouse or network error
    "unavailable",  # Availity returned an indeterminate / unsupported result
)

# Availity coverage status codes from the 271 response.
# "4" = eligible, others represent various partial/inactive states.
COVERAGE_STATUS_ACTIVE = "4"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class EligibilityResult(BaseModel):
    """Parsed eligibility data extracted from a 271 coverage response."""

    coverage_active: bool
    coverage_status_code: str | None = None

    # Plan info
    plan_name: str | None = None
    plan_begin_date: str | None = None  # ISO date YYYY-MM-DD
    plan_end_date: str | None = None
    group_number: str | None = None
    member_id: str | None = None

    # Financial (None = not disclosed / not applicable)
    copay_amount: float | None = None
    deductible_amount: float | None = None
    deductible_remaining: float | None = None
    out_of_pocket_max: float | None = None
    out_of_pocket_remaining: float | None = None

    # Referral / auth requirements
    referral_required: bool | None = None
    prior_auth_required: bool | None = None
    prior_auth_service_types: list[str] = []

    # Source badge for UI
    source: str = "availity"  # "availity" | "rules_engine" | "manual"


class AvailityPayer(BaseModel):
    """One entry from the Availity payer directory."""

    id: int | None = None
    availity_id: str  # e.g. "BCBSM"
    payer_name: str
    aliases: list[str] = []
    transaction_types: list[str] = []
    state_codes: list[str] = []
    last_synced_at: datetime | None = None


class EligibilityCheck(BaseModel):
    """Storage record for one eligibility inquiry attempt."""

    id: int | None = None

    # Scope (exactly one of these is set)
    scope_user_id: int | None = None
    scope_organization_id: int | None = None

    patient_id: int
    availity_payer_id: str  # Availity payer ID string (e.g. "BCBSM")
    payer_name: str | None = None
    service_type: str  # e.g. "30" (Health Benefit Plan Coverage)

    status: str  # ELIGIBILITY_CHECK_STATUS_VALUES
    error_message: str | None = None

    # Parsed result (None until status=complete)
    result: EligibilityResult | None = None

    # Raw 271 JSON stored for audit (not sent to UI)
    raw_response_json: str | None = None

    checked_at: datetime | None = None
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def parse_coverage_response(data: dict) -> EligibilityResult:
    """Parse an Availity /coverages response body into an EligibilityResult.

    The Availity 271 JSON is deeply nested.  This extracts the fields we
    care about for referral decisions and is intentionally lenient — missing
    keys return None rather than raising.
    """
    # Top-level coverage status
    status_code = data.get("coverageStatus") or data.get("statusCode")
    coverage_active = status_code == COVERAGE_STATUS_ACTIVE

    # Subscriber / member info (may be nested under "subscriber" or "patient")
    subscriber = data.get("subscriber") or data.get("patient") or {}
    member_id = subscriber.get("memberId") or data.get("memberId")

    # Plan info (often nested under "plans" list; take first)
    plans = data.get("plans") or []
    plan = plans[0] if plans else {}
    plan_name = plan.get("planName") or data.get("planName")
    group_number = plan.get("groupNumber") or data.get("groupNumber")
    plan_begin = plan.get("planBeginDate") or data.get("planBeginDate")
    plan_end = plan.get("planEndDate") or data.get("planEndDate")

    # Financial benefits (nested under plan["benefits"] list)
    copay = _extract_benefit_amount(plan, "co_payment", "30")
    deductible = _extract_benefit_amount(plan, "deductible", "30")
    deductible_remaining = _extract_benefit_amount(plan, "deductible_remaining", "30")
    oop_max = _extract_benefit_amount(plan, "out_of_pocket", "30")
    oop_remaining = _extract_benefit_amount(plan, "out_of_pocket_remaining", "30")

    # Referral / auth requirements
    referral_required = _extract_referral_required(data, plan)
    prior_auth_required, prior_auth_service_types = _extract_prior_auth(data, plan)

    return EligibilityResult(
        coverage_active=coverage_active,
        coverage_status_code=status_code,
        plan_name=plan_name,
        plan_begin_date=plan_begin,
        plan_end_date=plan_end,
        group_number=group_number,
        member_id=member_id,
        copay_amount=copay,
        deductible_amount=deductible,
        deductible_remaining=deductible_remaining,
        out_of_pocket_max=oop_max,
        out_of_pocket_remaining=oop_remaining,
        referral_required=referral_required,
        prior_auth_required=prior_auth_required,
        prior_auth_service_types=prior_auth_service_types,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _normalize_payer_name(name: str) -> str:
    """Normalize a payer name for fuzzy matching: lowercase, strip punctuation/spaces."""
    name = name.lower()
    # Remove common suffixes that differ across sources
    name = re.sub(r"\b(health\s*plan|insurance|inc\.?|llc\.?|corp\.?|co\.?)\b", "", name)
    name = re.sub(r"[^a-z0-9]", "", name)  # strip all non-alphanumeric
    return name.strip()


def match_payer_to_availity(
    payer_name: str,
    availity_payers: list["AvailityPayer"],
) -> str | None:
    """Return the `availity_id` of the best-matching payer, or None.

    Matching order:
    1. Exact case-insensitive match on payer_name or any alias
    2. Normalized substring match (after stripping common suffixes)

    Returns None when no payer matches with sufficient confidence.
    """
    if not payer_name or not availity_payers:
        return None

    needle_exact = payer_name.strip().lower()
    needle_norm = _normalize_payer_name(payer_name)

    # Pass 1: exact case-insensitive
    for p in availity_payers:
        candidates = [p.payer_name] + list(p.aliases)
        for c in candidates:
            if c.strip().lower() == needle_exact:
                return p.availity_id

    # Pass 2: normalized substring
    if len(needle_norm) >= 4:  # too-short tokens would over-match
        for p in availity_payers:
            candidates = [p.payer_name] + list(p.aliases)
            for c in candidates:
                c_norm = _normalize_payer_name(c)
                if needle_norm in c_norm or c_norm in needle_norm:
                    return p.availity_id

    return None


def _extract_benefit_amount(plan: dict, benefit_type: str, service_type: str) -> float | None:
    """Extract a dollar amount from a plan's benefits list by type."""
    benefits = plan.get("benefits") or []
    for b in benefits:
        if b.get("benefitType", "").lower() == benefit_type.lower():
            # May be keyed as "amounts" list or "amount" scalar
            amounts = b.get("amounts") or []
            if amounts:
                raw = amounts[0].get("value") or amounts[0].get("amount")
                if raw is not None:
                    try:
                        return float(raw)
                    except (TypeError, ValueError):
                        pass
            raw = b.get("value") or b.get("amount")
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
    return None


def overlay_eligibility(
    completeness: "CompletenessReportV2",
    check: EligibilityCheck,
) -> "CompletenessReportV2":
    """Annotate a completeness report with live eligibility data.

    Adds two synthetic items to the completeness report:
    - ``elig_coverage_active``: satisfied when the live check confirms active coverage
    - ``elig_referral_required``: satisfied when referral_required is False (i.e. no
      referral needed) or when an auth number is already present on the referral

    Items are appended so they appear after the baseline checks in the UI.
    Returns a new CompletenessReportV2 — does not mutate the original.
    """
    from docstats.domain.rules import CompletenessReportV2
    from docstats.domain.referrals import CompletenessItem

    if check.status != "complete" or check.result is None:
        return completeness

    r = check.result
    new_items = list(completeness.items)

    # Coverage active
    new_items.append(
        CompletenessItem(
            code="elig_coverage_active",
            label="Coverage active (live verified)",
            required=True,
            satisfied=r.coverage_active,
        )
    )

    # Referral required flag — if None (unknown), don't add the item
    if r.referral_required is not None:
        new_items.append(
            CompletenessItem(
                code="elig_referral_not_required",
                label="No referral required (live verified)"
                if not r.referral_required
                else "Referral required — authorization needed",
                required=False,
                satisfied=not r.referral_required,
            )
        )

    # Prior auth flag
    if r.prior_auth_required is not None:
        new_items.append(
            CompletenessItem(
                code="elig_prior_auth",
                label="Prior authorization not required (live verified)"
                if not r.prior_auth_required
                else "Prior authorization required",
                required=False,
                satisfied=not r.prior_auth_required,
            )
        )

    return CompletenessReportV2(
        items=new_items,
        red_flags=completeness.red_flags,
        recommended_attachments=completeness.recommended_attachments,
        rejection_hints=completeness.rejection_hints,
        specialty_display_name=completeness.specialty_display_name,
        payer_display_name=completeness.payer_display_name,
    )


def _extract_referral_required(data: dict, plan: dict) -> bool | None:
    """Determine if a referral is required from the coverage response."""
    # Some payers return a top-level flag; others bury it in benefit details.
    if "referralRequired" in data:
        return bool(data["referralRequired"])
    if "referralRequired" in plan:
        return bool(plan["referralRequired"])
    # Fall back to scanning benefit type descriptions
    benefits = plan.get("benefits") or []
    for b in benefits:
        desc = (b.get("description") or "").lower()
        if "referral" in desc and "required" in desc:
            return True
    return None


def _extract_prior_auth(data: dict, plan: dict) -> tuple[bool | None, list[str]]:
    """Return (prior_auth_required, service_types_requiring_auth)."""
    service_types: list[str] = []

    # Check for explicit flag
    required: bool | None = None
    if "priorAuthorizationRequired" in data:
        required = bool(data["priorAuthorizationRequired"])
    elif "priorAuthorizationRequired" in plan:
        required = bool(plan["priorAuthorizationRequired"])

    # Collect service types from benefits
    benefits = plan.get("benefits") or []
    for b in benefits:
        desc = (b.get("description") or "").lower()
        if "prior auth" in desc or "authorization" in desc:
            svc = b.get("serviceType") or b.get("serviceTypeCode")
            if svc:
                service_types.append(str(svc))
            if required is None:
                required = True

    return required, service_types
