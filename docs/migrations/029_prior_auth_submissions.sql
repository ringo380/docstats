-- Migration 029: Prior authorization submissions (X12 278) — Phase 11.E
--
-- One row per submission attempt against Availity's prior-auth API.
-- Rows are scope-owned (exactly one of scope_user_id / scope_organization_id
-- non-null, matching docstats_eligibility_checks).
--
-- Referral FK is RESTRICT — an in-flight auth blocks referral hard-delete
-- (referrals are evidence; can still be soft-deleted).

CREATE TABLE IF NOT EXISTS docstats_prior_auth_submissions (
    id                       BIGSERIAL PRIMARY KEY,

    scope_user_id            INTEGER REFERENCES docstats_users(id) ON DELETE SET NULL,
    scope_organization_id    INTEGER REFERENCES docstats_organizations(id) ON DELETE SET NULL,

    referral_id              INTEGER NOT NULL REFERENCES docstats_referrals(id) ON DELETE RESTRICT,

    availity_payer_id        TEXT    NOT NULL,
    payer_name               TEXT,
    member_id                TEXT    NOT NULL,
    service_type             TEXT    NOT NULL,

    diagnosis_codes_json     TEXT,           -- JSON array of ICD-10 codes
    procedure_codes_json     TEXT,           -- JSON array of CPT/HCPCS codes
    service_date             DATE,
    place_of_service         TEXT,

    status                   TEXT    NOT NULL
        CHECK (status IN ('pending','submitted','approved','denied','cancelled','error','unavailable')),
    availity_submission_id   TEXT,
    reference_number         TEXT,
    decision_date            TIMESTAMPTZ,
    decision_reason          TEXT,
    error_message            TEXT,
    idempotency_key          TEXT,

    raw_request_json         TEXT,
    raw_response_json        TEXT,

    submitted_at             TIMESTAMPTZ,
    last_polled_at           TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT prior_auth_submissions_scope_xor CHECK (
        (scope_user_id IS NULL) != (scope_organization_id IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_prior_auth_idempotency
    ON docstats_prior_auth_submissions (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_prior_auth_referral
    ON docstats_prior_auth_submissions (referral_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_prior_auth_scope_user
    ON docstats_prior_auth_submissions (scope_user_id, created_at DESC)
    WHERE scope_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_prior_auth_scope_org
    ON docstats_prior_auth_submissions (scope_organization_id, created_at DESC)
    WHERE scope_organization_id IS NOT NULL;
