"""Reference / configuration tables.

Three tables land here (Phase 1.E):

- ``insurance_plans`` — the payer/plan catalog. Scope-owned (user XOR org)
  just like ``patients``: solo users build their own catalog of the plans
  they carry; orgs maintain a shared catalog of the plans their patients
  bring in. Referrals FK into this via ``referrals.payer_plan_id``.

- ``specialty_rules`` — per-specialty requirements that drive the Phase 3
  rules engine (required fields, recommended attachments, urgency red flags,
  common rejection reasons). ``organization_id`` is nullable: NULL means
  "platform default", an integer means "org-specific override." Lookups
  merge the two, with org overrides winning per ``specialty_code``.

- ``payer_rules`` — per-payer auth/referral requirements (keyed by
  ``payer_key`` = ``{payer_name}|{plan_type}``). Same org-override pattern
  as specialty_rules.

Rule rows carry a ``version_id`` that admins bump when editing — the
rules engine uses it as a cache key so in-memory rule sets invalidate
cleanly on edit.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, Field

# Insurance plan types. Not exhaustive — "other" is the escape hatch; common
# US payer categories are enumerated so the referral builder can make
# reasonable auth-requirement defaults before payer-specific rules fire.
PLAN_TYPE_VALUES: Final[tuple[str, ...]] = (
    "hmo",
    "ppo",
    "pos",
    "epo",
    "medicare",
    "medicare_advantage",
    "medicaid",
    "tricare",
    "aca_marketplace",
    "self_pay",
    "other",
)

# Rule provenance. ``seed`` rows ship in the codebase (Phase 3 seed bundle);
# ``admin_override`` rows are written via the admin UI (Phase 6). A row with
# ``source=admin_override`` and ``organization_id=NULL`` would be a global
# rule edited by a platform admin — extremely rare, but valid.
RULE_SOURCE_VALUES: Final[tuple[str, ...]] = ("seed", "admin_override")


class InsurancePlan(BaseModel):
    """A payer + plan row. Scope-owned just like Patient."""

    id: int
    scope_user_id: int | None = None
    scope_organization_id: int | None = None

    payer_name: str
    plan_name: str | None = None
    plan_type: str = "other"  # must be in PLAN_TYPE_VALUES

    # Hints for form validation — e.g. Kaiser plans often start with "KP-".
    # Free-text regex-ish pattern; not enforced by storage.
    member_id_pattern: str | None = None
    group_id_pattern: str | None = None

    # Heuristic defaults for the referral wizard; the payer_rules engine
    # (when run) can override these per-service.
    requires_referral: bool = False
    requires_prior_auth: bool = False

    notes: str | None = None

    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class SpecialtyRule(BaseModel):
    """Specialty-specific requirements for a referral packet.

    ``organization_id = None`` is a platform-default rule; an integer is an
    org-specific override for the same ``specialty_code``. The rules engine
    in Phase 3 merges globals with the active scope's overrides.

    ``version_id`` bumps on every admin edit so the rules engine can cache
    compiled rule sets keyed by ``(organization_id, version_id)``.
    """

    id: int
    organization_id: int | None = None  # NULL = platform default
    specialty_code: str  # NUCC taxonomy code
    display_name: str | None = None

    # JSONB payloads — each a dict[str, Any]. The rules engine owns the
    # actual shape; storage just persists and returns the whole blob.
    required_fields: dict[str, Any] = Field(default_factory=dict)
    recommended_attachments: dict[str, Any] = Field(default_factory=dict)
    intake_questions: dict[str, Any] = Field(default_factory=dict)
    urgency_red_flags: dict[str, Any] = Field(default_factory=dict)
    common_rejection_reasons: dict[str, Any] = Field(default_factory=dict)

    source: str = "seed"  # must be in RULE_SOURCE_VALUES
    version_id: int = 1

    created_at: datetime
    updated_at: datetime


class PayerRule(BaseModel):
    """Per-payer auth/referral rules.

    ``payer_key`` is a synthetic identifier (``"{payer_name}|{plan_type}"``)
    so the rules engine can look up by a single string. Two rows with the
    same ``payer_key`` and different ``organization_id`` represent the
    platform-default rule and an org override for the same payer.
    """

    id: int
    organization_id: int | None = None  # NULL = platform default
    payer_key: str  # canonical identifier
    display_name: str | None = None

    referral_required: bool = False
    auth_required_services: dict[str, Any] = Field(default_factory=dict)
    auth_typical_turnaround_days: int | None = None
    records_required: dict[str, Any] = Field(default_factory=dict)

    notes: str | None = None
    source: str = "seed"  # must be in RULE_SOURCE_VALUES
    version_id: int = 1

    created_at: datetime
    updated_at: datetime
