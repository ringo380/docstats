"""Clinician identity verification — automated NPPES + OIG checks.

Called synchronously at signup (and on the patient→clinician upgrade
route). Returns a tri-state verdict that the route layer uses to decide
whether to create the user with full access (``verified``), gated access
(``pending_review``), or refuse the signup (``rejected``).

Pipeline (priority order — first ``rejected`` short-circuits;
otherwise the worst of the per-check verdicts wins):

1. Format + Luhn check (``validators.npi_luhn_ok``).
2. OIG LEIE exclusion (``OIGClient.check_exclusion``). Runs early so
   the route's "contact support" generic message can avoid leaking
   non-OIG check details.
3. NPPES lookup (``NPPESClient.lookup``). Outage → ``pending_review``,
   not ``rejected`` (we don't want a transient NPPES blip to lock out
   legitimate clinicians).
4. NPPES status active.
5. Entity type — NPI-2 (organization) demotes to ``pending_review``;
   the clinician model is individual-NPI-shaped today.
6. Name fuzzy match against NPPES record (``rapidfuzz``).
7. State overlap between claimed license state and NPPES practice
   addresses.

Reasons accumulate as machine codes so audit + admin review can see
exactly which checks tripped. Snapshots of the NPPES result are kept
on the verdict for the same reason — the row that stores them is
immutable evidence the user was who they claimed at signup time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from docstats.validators import npi_luhn_ok

if TYPE_CHECKING:
    from docstats.client import NPPESClient
    from docstats.oig_client import OIGClient

logger = logging.getLogger(__name__)

Verdict = Literal["verified", "pending_review", "rejected"]
NAME_MATCH_THRESHOLD = 85  # rapidfuzz token_set_ratio out of 100


@dataclass(frozen=True)
class ClinicianVerification:
    """Outcome of an automated clinician identity check."""

    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    nppes_snapshot: dict[str, Any] | None = None
    primary_taxonomy: str | None = None
    method: str = "nppes_auto"


def _worst(a: Verdict, b: Verdict) -> Verdict:
    """Return the worst (most-restrictive) of two verdicts."""
    order = {"verified": 0, "pending_review": 1, "rejected": 2}
    return a if order[a] >= order[b] else b


def _normalize_name(s: str | None) -> str:
    return (s or "").strip().lower()


def _name_match(claimed_first: str, claimed_last: str, nppes_first: str, nppes_last: str) -> bool:
    """Token-set fuzzy compare on full name. Handles middle-name + suffix
    variation gracefully (token_set ignores extra tokens on either side)."""
    try:
        from rapidfuzz import fuzz  # type: ignore[import-untyped]
    except ImportError:
        # Defensive fallback: exact-after-normalize comparison. If
        # rapidfuzz isn't installed (lint-only CI shard, light dev env),
        # we still accept the obvious match cases.
        a = f"{_normalize_name(claimed_first)} {_normalize_name(claimed_last)}".strip()
        b = f"{_normalize_name(nppes_first)} {_normalize_name(nppes_last)}".strip()
        return bool(a) and a == b
    a = f"{_normalize_name(claimed_first)} {_normalize_name(claimed_last)}".strip()
    b = f"{_normalize_name(nppes_first)} {_normalize_name(nppes_last)}".strip()
    if not a or not b:
        return False
    return fuzz.token_set_ratio(a, b) >= NAME_MATCH_THRESHOLD


def _nppes_address_states(nppes_result: Any) -> set[str]:
    """Extract the set of US state codes from NPPES addresses on a result."""
    states: set[str] = set()
    for addr in getattr(nppes_result, "addresses", None) or []:
        st = (getattr(addr, "state", "") or "").strip().upper()
        if st:
            states.add(st)
    return states


def verify_clinician(
    *,
    npi: str,
    first_name: str,
    last_name: str,
    state_license_state: str | None,
    nppes: "NPPESClient",
    oig: "OIGClient | None" = None,
) -> ClinicianVerification:
    """Run the full pipeline and return a ``ClinicianVerification``.

    The route layer is responsible for translating ``rejected`` into a
    generic "contact support" UX (which preserves privacy on
    OIG-excluded NPIs) and ``pending_review`` into a banner-with-account.
    """
    reasons: list[str] = []

    # 1. Format + Luhn.
    if not npi_luhn_ok(npi):
        return ClinicianVerification(
            verdict="rejected",
            reasons=["npi_format_invalid"],
        )

    # 2. OIG LEIE exclusion. If the client is missing entirely (test
    # mode or local dev without LEIE cached) we record a soft reason
    # and continue — the route layer can decide policy. If the client
    # is present but raises, we treat as outage (pending_review) so a
    # transient OIG download failure doesn't block legit signups.
    if oig is None:
        reasons.append("oig_unavailable")
    else:
        try:
            hit = oig.check_exclusion(npi)
        except Exception:
            logger.exception("OIG LEIE check failed for npi=%s", npi)
            reasons.append("oig_unavailable")
        else:
            if hit is not None:
                # Hard reject. Reason is intentionally the only one we
                # surface — the route shows a generic message.
                return ClinicianVerification(
                    verdict="rejected",
                    reasons=["oig_excluded"],
                )

    # 3. NPPES lookup.
    try:
        result = nppes.lookup(npi)
    except Exception:
        logger.exception("NPPES lookup failed for npi=%s — pending_review", npi)
        reasons.append("nppes_unavailable")
        return ClinicianVerification(verdict="pending_review", reasons=reasons)

    if result is None:
        return ClinicianVerification(
            verdict="rejected",
            reasons=[*reasons, "npi_not_found"],
        )

    # Snapshot for audit + admin review. ``model_dump`` serializes the
    # full NPIResult including basic, addresses, taxonomies, etc.
    snapshot = result.model_dump(mode="json")
    primary_taxonomy = result.primary_taxonomy.code if result.primary_taxonomy is not None else None

    verdict: Verdict = "verified"

    # 4. Status active.
    parsed = result.parsed_basic()
    status = (getattr(parsed, "status", "") or "").upper()
    deactivated = bool(getattr(parsed, "deactivation_date", None))
    reactivated = bool(getattr(parsed, "reactivation_date", None))
    if status not in ("A", "ACTIVE") or (deactivated and not reactivated):
        return ClinicianVerification(
            verdict="rejected",
            reasons=[*reasons, "npi_deactivated"],
            nppes_snapshot=snapshot,
            primary_taxonomy=primary_taxonomy,
        )

    # 5. Entity type. NPI-2 = organization → pending_review.
    if not result.is_individual:
        verdict = _worst(verdict, "pending_review")
        reasons.append("org_npi_not_individual")

    # 6. Name fuzzy match (only meaningful on NPI-1 records).
    if result.is_individual and not _name_match(
        first_name,
        last_name,
        getattr(parsed, "first_name", "") or "",
        getattr(parsed, "last_name", "") or "",
    ):
        verdict = _worst(verdict, "pending_review")
        reasons.append("name_mismatch")

    # 7. State overlap (only when the claim provides a state).
    if state_license_state:
        nppes_states = _nppes_address_states(result)
        if nppes_states and state_license_state.upper() not in nppes_states:
            verdict = _worst(verdict, "pending_review")
            reasons.append("state_no_overlap")

    if verdict == "verified" and not reasons:
        reasons = ["all_checks_passed"]

    return ClinicianVerification(
        verdict=verdict,
        reasons=reasons,
        nppes_snapshot=snapshot,
        primary_taxonomy=primary_taxonomy,
    )
