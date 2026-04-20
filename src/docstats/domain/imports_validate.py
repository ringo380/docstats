"""Row-level CSV-import validation (Phase 4.C).

Single entry point: :func:`validate_row(raw, mapping, *, storage, scope)`.
Returns a dict of ``{target_field: error_message}`` — empty = row is valid.
The route layer consumes the result to update ``csv_import_rows.status``
and ``csv_import_rows.validation_errors``.

Framework-free — no FastAPI imports — so tests can exercise validators in
isolation. The rules-engine lookup is cheap now that Phase 3 follow-up
made ``list_specialty_rules(specialty_code=...)`` DB-level-filtered, but
the caller can still pass a ``specialty_cache`` to memoize the N-row loop.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from docstats.domain.reference import SpecialtyRule
from docstats.domain.referrals import URGENCY_VALUES
from docstats.domain.rules import resolve_specialty_rule
from docstats.scope import Scope
from docstats.storage_base import StorageBase
from docstats.validators import ValidationError, validate_npi

# Target fields the validator treats as ALWAYS required (regardless of the
# picked specialty). These map to what we need just to write the referral
# + patient rows — below this bar there's nothing to commit.
ALWAYS_REQUIRED_TARGETS: tuple[str, ...] = (
    "patient_first_name",
    "patient_last_name",
    "reason",
)


def _get(raw: dict[str, Any], mapping: dict[str, str], target: str) -> str:
    """Return the stripped cell value for ``target``, or ''.

    ``mapping`` is ``{target_field: csv_header}``; an unmapped target or
    a missing cell collapses to empty-string so the ``required`` branch
    fires naturally.
    """
    header = mapping.get(target)
    if not header:
        return ""
    value = raw.get(header)
    if value is None:
        return ""
    return str(value).strip()


def validate_row(
    raw: dict[str, Any],
    mapping: dict[str, str],
    *,
    storage: StorageBase,
    scope: Scope,
    specialty_cache: dict[str, SpecialtyRule | None] | None = None,
) -> dict[str, str]:
    """Run per-row validators. Empty dict = valid; non-empty = errored row.

    Validators run in order so a more specific error shadows a generic one
    (e.g. a malformed NPI reports "must be 10 digits" rather than falling
    through to "required by Cardiology").
    """
    errors: dict[str, str] = {}

    # 1. Always-required fields (independent of specialty).
    for target in ALWAYS_REQUIRED_TARGETS:
        if not _get(raw, mapping, target):
            errors[target] = "required"

    # 2. Enum value for urgency (defaults to "routine" if unmapped/blank).
    urgency = _get(raw, mapping, "urgency") or "routine"
    if urgency not in URGENCY_VALUES:
        errors["urgency"] = f"must be one of {', '.join(URGENCY_VALUES)}"

    # 3. NPI format check on both receiving + referring.
    for field in ("receiving_provider_npi", "referring_provider_npi"):
        npi = _get(raw, mapping, field)
        if npi:
            try:
                validate_npi(npi)
            except ValidationError:
                errors[field] = "must be 10 digits"

    # 4. Date-of-birth must be ISO YYYY-MM-DD if present. Future dates are
    # always typos — reject them here to match onboarding + patient-form
    # behavior.
    dob = _get(raw, mapping, "patient_dob")
    if dob:
        try:
            parsed_dob = date.fromisoformat(dob)
        except ValueError:
            errors["patient_dob"] = "must be YYYY-MM-DD"
        else:
            if parsed_dob > date.today():
                errors["patient_dob"] = "cannot be in the future"

    # 5. Specialty-driven required fields. The rules-engine resolver uses the
    # DB-level narrowing shipped in the Phase 3 review follow-up, so this is
    # at-most-two-rows per unique specialty_code. Cache across rows anyway so
    # a file with 2000 cardiology rows does one lookup, not 2000.
    specialty_code = _get(raw, mapping, "specialty_code")
    if specialty_code:
        if specialty_cache is not None and specialty_code in specialty_cache:
            rule = specialty_cache[specialty_code]
        else:
            rule = resolve_specialty_rule(storage, scope.organization_id, specialty_code)
            if specialty_cache is not None:
                specialty_cache[specialty_code] = rule
        if rule is not None:
            required = rule.required_fields.get("fields", []) if rule.required_fields else []
            for req_field in required:
                if not isinstance(req_field, str):
                    continue
                # Don't overwrite a more-specific error (bad NPI etc.)
                if req_field in errors:
                    continue
                if not _get(raw, mapping, req_field):
                    errors[req_field] = f"required by {rule.display_name or specialty_code}"

    return errors
