"""Specialty + payer rules engine (Phase 3).

Layers three data sources into a single :class:`CompletenessReport`:

1. The baseline_completeness check from ``domain.referrals`` (always runs).
2. The matched :class:`SpecialtyRule` (platform default, possibly overridden
   by an org-specific row).
3. The matched :class:`PayerRule` (same global/override pattern, keyed by
   ``{payer_name}|{plan_type}``).

Rule rows are seeded by :mod:`docstats.domain.seed`. Fetch + merge is
``resolve_ruleset(storage, scope, specialty_code, payer_plan)``. Evaluation
is ``evaluate(referral, ruleset)``.

The engine is framework-free — no FastAPI / Jinja imports — so tests can
exercise it in isolation. Routes wire it in via ``rules_based_completeness``
in ``routes/referrals.py``.

Memoization is intentionally NOT implemented in this module — routes re-run
``evaluate`` per request, and the cost is bounded by the DB-level
``specialty_code=`` / ``payer_key=`` narrowing on ``list_*_rules`` (at most
2 rows fetched per resolve). If request volume ever makes this a hotspot,
key a module-level LRU cache on ``(referral.id, referral.updated_at,
specialty_rule.id, specialty_rule.version_id, payer_rule.id,
payer_rule.version_id)`` — the rule rows' ``version_id`` bumps on every
admin edit, so the cache invalidates cleanly without manual busting.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from docstats.domain.reference import InsurancePlan, PayerRule, SpecialtyRule
from docstats.domain.referrals import (
    CompletenessItem,
    Referral,
    baseline_completeness,
)
from docstats.scope import Scope
from docstats.storage_base import StorageBase

# Map from referral_fields in rule.required_fields["fields"] to a
# (label, satisfaction-check) pair. Keeping the map explicit avoids a
# runtime-getattr loop that silently no-ops on a typo.
_REQUIRED_FIELD_CHECKS: dict[str, str] = {
    "reason": "Reason for referral",
    "clinical_question": "Specific clinical question",
    "diagnosis_primary_icd": "Primary diagnosis (ICD-10)",
    "receiving_provider_npi": "Receiving provider NPI",
    "receiving_organization_name": "Receiving organization name",
    "specialty_desc": "Specialty description",
    "urgency": "Urgency",
    "authorization_number": "Authorization number",
    "authorization_status": "Authorization status",
    "referring_provider_name": "Referring provider name",
}


@dataclass(frozen=True)
class ResolvedRuleSet:
    """The effective specialty + payer rule pair for a referral.

    Either field can be ``None`` — we still evaluate the baseline and any
    rule that did match. Missing both rules means the caller falls through
    to baseline-only behavior.
    """

    specialty: SpecialtyRule | None
    payer: PayerRule | None


class CompletenessReportV2(BaseModel):
    """Rules-aware completeness report.

    Kept as a separate model (not a subclass) so the baseline
    :class:`CompletenessReport` stays simple for callers that don't want
    rules. Route handlers that call :func:`evaluate` render this directly.
    """

    items: list[CompletenessItem]
    # Urgency red-flag keywords that matched ``reason`` or
    # ``clinical_question``. Coordinators should flag-escalate the referral.
    red_flags: list[str]
    # Human-readable attachment labels the specialty rule recommends. Surfaced
    # verbatim — a Phase 2.D follow-up will cross-check against actual
    # referral_attachments sub-entity rows.
    recommended_attachments: list[str]
    # Free-text "avoid these" hints from common_rejection_reasons.
    rejection_hints: list[str]
    # For UI echo — which rules actually matched.
    specialty_display_name: str | None = None
    payer_display_name: str | None = None

    @property
    def missing_required(self) -> list[CompletenessItem]:
        return [i for i in self.items if i.required and not i.satisfied]

    @property
    def missing_recommended(self) -> list[CompletenessItem]:
        return [i for i in self.items if not i.required and not i.satisfied]

    @property
    def is_complete(self) -> bool:
        """True when no required items are missing.

        Red flags are reported via :attr:`red_flags` and surface as a separate
        UI section — they do NOT gate completeness, since a red-flag match
        triggers an urgency escalation rather than a missing-data block. Keep
        the strict "all required present" reading here; let the UI decide
        what to do with the escalation signal.
        """
        return not self.missing_required


def resolve_specialty_rule(
    storage: StorageBase,
    organization_id: int | None,
    specialty_code: str | None,
) -> SpecialtyRule | None:
    """Look up the effective specialty rule.

    Passes ``specialty_code`` to ``list_specialty_rules`` so the DB narrows to
    at most two rows (global + any org override). Storage contract orders
    ``(specialty_code, organization_id NULLS FIRST, id)``, so the last row in
    the filtered result is the effective rule (org override if present,
    global otherwise).
    """
    if not specialty_code:
        return None
    rows = storage.list_specialty_rules(
        organization_id=organization_id,
        include_globals=True,
        specialty_code=specialty_code,
    )
    return rows[-1] if rows else None


def resolve_payer_rule(
    storage: StorageBase,
    organization_id: int | None,
    payer_plan: InsurancePlan | None,
) -> PayerRule | None:
    """Derive ``payer_key`` from the plan row and look up the rule.

    ``payer_key`` is ``"{payer_name}|{plan_type}"`` — the canonical seed
    format. ``create_insurance_plan`` rejects ``|`` in ``payer_name`` at the
    storage boundary so the derived key is unambiguous.
    """
    if payer_plan is None:
        return None
    payer_key = f"{payer_plan.payer_name}|{payer_plan.plan_type}"
    rows = storage.list_payer_rules(
        organization_id=organization_id,
        include_globals=True,
        payer_key=payer_key,
    )
    return rows[-1] if rows else None


def resolve_ruleset(
    storage: StorageBase,
    scope: Scope,
    referral: Referral,
) -> ResolvedRuleSet:
    """Convenience — resolve both rules for a given referral in the caller's scope."""
    payer_plan: InsurancePlan | None = None
    if referral.payer_plan_id is not None:
        payer_plan = storage.get_insurance_plan(scope, referral.payer_plan_id)
    return ResolvedRuleSet(
        specialty=resolve_specialty_rule(storage, scope.organization_id, referral.specialty_code),
        payer=resolve_payer_rule(storage, scope.organization_id, payer_plan),
    )


def _nonblank(v: str | None) -> bool:
    return bool(v and v.strip())


def _check_referral_field(referral: Referral, field_name: str) -> bool:
    """Return True iff ``referral.<field_name>`` is non-blank."""
    value = getattr(referral, field_name, None)
    return _nonblank(value) if isinstance(value, str) else value is not None


def detect_red_flags_in_text(
    reason: str | None,
    clinical_question: str | None,
    specialty: SpecialtyRule | None,
) -> list[str]:
    """Return red-flag keywords that matched reason/clinical_question text.

    Case-insensitive substring match — the SPECIALTY_DEFAULTS keyword list
    is short and specific enough ("cauda equina", "chest pain") that a
    substring scan won't false-positive in practice.

    Takes the two text fields directly so callers can invoke it BEFORE a
    Referral object exists (e.g. pre-create in the POST /referrals handler
    for auto-urgency escalation).
    """
    if specialty is None:
        return []
    keywords = (
        specialty.urgency_red_flags.get("keywords", []) if specialty.urgency_red_flags else []
    )
    if not keywords:
        return []
    haystack = f"{reason or ''} {clinical_question or ''}".lower()
    if not haystack.strip():
        return []
    hits: list[str] = []
    for kw in keywords:
        if isinstance(kw, str) and kw.strip() and kw.lower() in haystack:
            hits.append(kw)
    return hits


def detect_red_flags(referral: Referral, specialty: SpecialtyRule | None) -> list[str]:
    """Referral-object convenience wrapper around :func:`detect_red_flags_in_text`."""
    return detect_red_flags_in_text(referral.reason, referral.clinical_question, specialty)


def evaluate(referral: Referral, ruleset: ResolvedRuleSet) -> CompletenessReportV2:
    """Full completeness check — baseline + specialty overlay + payer overlay.

    The baseline items always appear first so the UI's "required" section
    stays stable. Specialty-required fields append NEW items only — a field
    already covered by baseline (e.g. ``reason``) won't double-report.

    Payer rules contribute ``referral_required`` / ``auth_required_services``
    hints via ``rejection_hints`` today — when Phase 11 ships eligibility
    checks they'll turn into hard required items.
    """
    items: list[CompletenessItem] = list(baseline_completeness(referral).items)
    # Map baseline field → baseline code so we can promote a recommended
    # baseline item to required when a specialty rule asks for it (instead
    # of adding a duplicate item).
    _FIELD_TO_BASELINE_CODE: dict[str, str] = {
        "reason": "reason",
        "specialty_desc": "specialty",
        "clinical_question": "clinical_question",
        "diagnosis_primary_icd": "primary_diagnosis",
        "receiving_provider_npi": "receiving_side",
        "receiving_organization_name": "receiving_side",
        "referring_provider_name": "referring_side",
    }
    items_by_code: dict[str, int] = {i.code: idx for idx, i in enumerate(items)}

    specialty = ruleset.specialty
    payer = ruleset.payer

    if specialty is not None:
        spec_required = (
            specialty.required_fields.get("fields", []) if specialty.required_fields else []
        )
        for field_name in spec_required:
            if not isinstance(field_name, str) or field_name not in _REQUIRED_FIELD_CHECKS:
                continue
            baseline_code = _FIELD_TO_BASELINE_CODE.get(field_name)
            if baseline_code and baseline_code in items_by_code:
                # Promote the baseline item to required (if it wasn't already).
                idx = items_by_code[baseline_code]
                existing = items[idx]
                if not existing.required:
                    items[idx] = CompletenessItem(
                        code=existing.code,
                        label=existing.label,
                        required=True,
                        satisfied=existing.satisfied,
                    )
                continue
            code = f"specialty_required_{field_name}"
            if code in items_by_code:
                continue
            items.append(
                CompletenessItem(
                    code=code,
                    label=_REQUIRED_FIELD_CHECKS[field_name],
                    required=True,
                    satisfied=_check_referral_field(referral, field_name),
                )
            )
            items_by_code[code] = len(items) - 1

    # Red flags — keyword match against reason + clinical_question.
    red_flags = detect_red_flags(referral, specialty)

    # Recommended attachments — surface labels; Phase 2 follow-up will check
    # them against actual referral_attachments rows.
    recommended_attachments: list[str] = []
    if specialty is not None and specialty.recommended_attachments:
        labels = specialty.recommended_attachments.get("labels", [])
        if isinstance(labels, list):
            recommended_attachments = [str(x) for x in labels if isinstance(x, str)]

    # Rejection hints — combine specialty + payer.
    rejection_hints: list[str] = []
    if specialty is not None and specialty.common_rejection_reasons:
        reasons = specialty.common_rejection_reasons.get("reasons", [])
        if isinstance(reasons, list):
            rejection_hints.extend(str(x) for x in reasons if isinstance(x, str))
    if payer is not None:
        if payer.referral_required and not _nonblank(referral.authorization_number):
            rejection_hints.append(
                f"{payer.display_name or payer.payer_key} typically requires a referral / authorization number"
            )
        if payer.records_required:
            kinds = payer.records_required.get("kinds", [])
            if isinstance(kinds, list) and kinds:
                rejection_hints.append(
                    f"{payer.display_name or payer.payer_key} expects: "
                    + ", ".join(str(k) for k in kinds if isinstance(k, str))
                )

    return CompletenessReportV2(
        items=items,
        red_flags=red_flags,
        recommended_attachments=recommended_attachments,
        rejection_hints=rejection_hints,
        specialty_display_name=specialty.display_name if specialty is not None else None,
        payer_display_name=(payer.display_name if payer is not None else None),
    )


def rules_based_completeness(
    storage: StorageBase,
    scope: Scope,
    referral: Referral,
) -> CompletenessReportV2:
    """Convenience: resolve ruleset + evaluate in one call."""
    return evaluate(referral, resolve_ruleset(storage, scope, referral))
