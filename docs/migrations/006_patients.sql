-- Phase 1.A: patients table — first-class scope-enforced entity.
--
-- Every row carries exactly one of scope_user_id / scope_organization_id
-- (solo mode / org mode). CHECK constraint enforces the XOR.
--
-- Apply via Supabase Management API:
--   curl -sS -X POST \
--     "https://api.supabase.com/v1/projects/uhnymifvdauzlmaogjfj/database/query" \
--     -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
--     -H "Content-Type: application/json" \
--     --data "$(jq -Rs '{query: .}' docs/migrations/006_patients.sql)"

CREATE TABLE IF NOT EXISTS docstats_patients (
    id                        BIGSERIAL PRIMARY KEY,
    -- Exactly one of these two must be set (enforced by CHECK below).
    scope_user_id             INTEGER REFERENCES docstats_users(id) ON DELETE CASCADE,
    scope_organization_id     INTEGER REFERENCES docstats_organizations(id) ON DELETE CASCADE,

    first_name                TEXT NOT NULL,
    last_name                 TEXT NOT NULL,
    middle_name               TEXT,
    date_of_birth             DATE,
    sex                       TEXT,
    mrn                       TEXT,

    preferred_language        TEXT,
    pronouns                  TEXT,

    phone                     TEXT,
    email                     TEXT,

    address_line1             TEXT,
    address_line2             TEXT,
    address_city              TEXT,
    address_state             TEXT,
    address_zip               TEXT,

    emergency_contact_name    TEXT,
    emergency_contact_phone   TEXT,

    notes                     TEXT,

    created_by_user_id        INTEGER REFERENCES docstats_users(id) ON DELETE SET NULL,
    created_at                TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at                TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    deleted_at                TIMESTAMP WITH TIME ZONE,

    CONSTRAINT docstats_patients_scope_exactly_one CHECK (
        (scope_user_id IS NOT NULL AND scope_organization_id IS NULL)
        OR (scope_user_id IS NULL AND scope_organization_id IS NOT NULL)
    )
);

-- Name/search indices on live rows — drives /patients list and referral
-- creation patient-picker.
CREATE INDEX IF NOT EXISTS idx_docstats_patients_scope_user_name
    ON docstats_patients (scope_user_id, last_name, first_name)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_docstats_patients_scope_org_name
    ON docstats_patients (scope_organization_id, last_name, first_name)
    WHERE deleted_at IS NULL;

-- MRN is an org-scoped identifier. Unique within an org's live rows, so
-- soft-deleting a patient frees the MRN for reuse on a re-admit.
CREATE UNIQUE INDEX IF NOT EXISTS idx_docstats_patients_org_mrn
    ON docstats_patients (scope_organization_id, mrn)
    WHERE scope_organization_id IS NOT NULL
      AND mrn IS NOT NULL
      AND deleted_at IS NULL;
