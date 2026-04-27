-- Migration 022: ehr_connections — encrypted SMART-on-FHIR token storage
--
-- Each row is one user's authorization to one EHR vendor sandbox/instance.
-- Tokens are stored as Fernet ciphertext (EHR_TOKEN_KEY env). One active row
-- per (user_id, ehr_vendor) is enforced in the storage layer via the same
-- race-safe pattern as staff_access_grants: revoke ALL active rows on new
-- connect (not by row_id) to close the Postgres TOCTOU window.
--
-- Vendor enum starts with 'epic_sandbox' only; CHECK widens with future
-- migrations as additional vendors land.

CREATE TABLE IF NOT EXISTS docstats_ehr_connections (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL
                            REFERENCES docstats_users(id) ON DELETE CASCADE,
    ehr_vendor          TEXT NOT NULL CHECK (ehr_vendor IN ('epic_sandbox')),
    iss                 TEXT NOT NULL,
    patient_fhir_id     TEXT,
    access_token_enc    TEXT NOT NULL,
    refresh_token_enc   TEXT,
    expires_at          TIMESTAMPTZ NOT NULL,
    scope               TEXT NOT NULL,
    revoked_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ehr_connections_user_active
    ON docstats_ehr_connections (user_id, ehr_vendor)
    WHERE revoked_at IS NULL;
