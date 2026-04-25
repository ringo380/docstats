-- Phase 11.A: Eligibility checks table
--
-- Stores real-time insurance eligibility inquiry results (X12 270/271)
-- fetched via the Availity Healthcare HIPAA Transactions API.
--
-- Scope ownership follows the same pattern as patients/referrals:
--   exactly one of scope_user_id / scope_organization_id is set.
--   The DB-level CHECK enforces this.
--
-- The (patient_id, availity_payer_id, service_type, checked_at) index
-- supports the "latest check for this patient+payer+service" query used
-- by the UI without a full table scan.

CREATE TABLE IF NOT EXISTS eligibility_checks (
    id                      BIGSERIAL PRIMARY KEY,

    -- Scope (exactly one must be non-null)
    scope_user_id           INTEGER REFERENCES users(id) ON DELETE SET NULL,
    scope_organization_id   INTEGER REFERENCES organizations(id) ON DELETE SET NULL,

    -- Subject of the inquiry
    patient_id              INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    availity_payer_id       TEXT    NOT NULL,   -- Availity payer ID string
    payer_name              TEXT,               -- human-readable label (denormalized)
    service_type            TEXT    NOT NULL,   -- X12 service type code e.g. "30"

    -- Status lifecycle
    status                  TEXT    NOT NULL
        CHECK (status IN ('pending','complete','error','unavailable')),
    error_message           TEXT,

    -- Parsed result (JSONB / TEXT storing EligibilityResult JSON)
    result_json             TEXT,               -- null until status='complete'

    -- Raw clearinghouse response for audit / support
    raw_response_json       TEXT,

    -- Timestamps
    checked_at              TIMESTAMPTZ,        -- when the API call completed
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Scope constraint
    CONSTRAINT eligibility_checks_scope_xor CHECK (
        (scope_user_id IS NULL) != (scope_organization_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_eligibility_checks_patient_payer
    ON eligibility_checks (patient_id, availity_payer_id, service_type, checked_at DESC);

CREATE INDEX IF NOT EXISTS ix_eligibility_checks_scope_user
    ON eligibility_checks (scope_user_id, created_at DESC)
    WHERE scope_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_eligibility_checks_scope_org
    ON eligibility_checks (scope_organization_id, created_at DESC)
    WHERE scope_organization_id IS NOT NULL;
