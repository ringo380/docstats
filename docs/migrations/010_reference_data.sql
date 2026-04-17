-- Phase 1.E: reference / configuration tables.
--
-- Three tables:
--   * insurance_plans — scope-owned (user XOR org) payer catalog
--   * specialty_rules — org override OR platform default (organization_id NULL)
--   * payer_rules    — same org-override pattern as specialty_rules
--
-- Apply via Supabase Management API:
--   curl -sS -X POST \
--     "https://api.supabase.com/v1/projects/uhnymifvdauzlmaogjfj/database/query" \
--     -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
--     -H "Content-Type: application/json" \
--     --data "$(jq -Rs '{query: .}' docs/migrations/010_reference_data.sql)"

-- --- Insurance plans (scope-owned) ---

CREATE TABLE IF NOT EXISTS docstats_insurance_plans (
    id                         BIGSERIAL PRIMARY KEY,
    scope_user_id              INTEGER REFERENCES docstats_users(id) ON DELETE CASCADE,
    scope_organization_id      INTEGER REFERENCES docstats_organizations(id) ON DELETE CASCADE,

    payer_name                 TEXT NOT NULL,
    plan_name                  TEXT,
    plan_type                  TEXT NOT NULL DEFAULT 'other'
                                CHECK (plan_type IN
                                       ('hmo','ppo','pos','epo','medicare','medicare_advantage',
                                        'medicaid','tricare','aca_marketplace','self_pay','other')),
    member_id_pattern          TEXT,
    group_id_pattern           TEXT,
    requires_referral          BOOLEAN NOT NULL DEFAULT FALSE,
    requires_prior_auth        BOOLEAN NOT NULL DEFAULT FALSE,
    notes                      TEXT,

    created_at                 TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    deleted_at                 TIMESTAMP WITH TIME ZONE,

    CONSTRAINT docstats_insurance_plans_scope_exactly_one CHECK (
        (scope_user_id IS NOT NULL AND scope_organization_id IS NULL)
        OR (scope_user_id IS NULL AND scope_organization_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_docstats_insurance_plans_scope_user
    ON docstats_insurance_plans (scope_user_id, payer_name)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_docstats_insurance_plans_scope_org
    ON docstats_insurance_plans (scope_organization_id, payer_name)
    WHERE deleted_at IS NULL;


-- --- Specialty rules (org override or platform default) ---

CREATE TABLE IF NOT EXISTS docstats_specialty_rules (
    id                         BIGSERIAL PRIMARY KEY,
    -- NULL = platform default; FK to an org = that org's override.
    organization_id            INTEGER REFERENCES docstats_organizations(id) ON DELETE CASCADE,
    specialty_code             TEXT NOT NULL,
    display_name               TEXT,

    required_fields            JSONB NOT NULL DEFAULT '{}'::jsonb,
    recommended_attachments    JSONB NOT NULL DEFAULT '{}'::jsonb,
    intake_questions           JSONB NOT NULL DEFAULT '{}'::jsonb,
    urgency_red_flags          JSONB NOT NULL DEFAULT '{}'::jsonb,
    common_rejection_reasons   JSONB NOT NULL DEFAULT '{}'::jsonb,

    source                     TEXT NOT NULL DEFAULT 'seed'
                                CHECK (source IN ('seed','admin_override')),
    version_id                 INTEGER NOT NULL DEFAULT 1,

    created_at                 TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

-- One platform-default row per specialty_code; one org-override row per
-- (organization_id, specialty_code). Two partial indices handle the NULL
-- case (global) and the non-NULL case (org-specific) separately.
CREATE UNIQUE INDEX IF NOT EXISTS idx_docstats_specialty_rules_global_code
    ON docstats_specialty_rules (specialty_code)
    WHERE organization_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_docstats_specialty_rules_org_code
    ON docstats_specialty_rules (organization_id, specialty_code)
    WHERE organization_id IS NOT NULL;


-- --- Payer rules (org override or platform default) ---

CREATE TABLE IF NOT EXISTS docstats_payer_rules (
    id                              BIGSERIAL PRIMARY KEY,
    organization_id                 INTEGER REFERENCES docstats_organizations(id) ON DELETE CASCADE,
    payer_key                       TEXT NOT NULL,  -- e.g. "Kaiser Permanente|hmo"
    display_name                    TEXT,

    referral_required               BOOLEAN NOT NULL DEFAULT FALSE,
    auth_required_services          JSONB NOT NULL DEFAULT '{}'::jsonb,
    auth_typical_turnaround_days    INTEGER,
    records_required                JSONB NOT NULL DEFAULT '{}'::jsonb,

    notes                           TEXT,
    source                          TEXT NOT NULL DEFAULT 'seed'
                                     CHECK (source IN ('seed','admin_override')),
    version_id                      INTEGER NOT NULL DEFAULT 1,

    created_at                      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at                      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_docstats_payer_rules_global_key
    ON docstats_payer_rules (payer_key)
    WHERE organization_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_docstats_payer_rules_org_key
    ON docstats_payer_rules (organization_id, payer_key)
    WHERE organization_id IS NOT NULL;
