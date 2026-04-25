-- Phase 11.D: Availity payer directory cache.
--
-- availity_payers stores a local copy of the Availity /payers directory so
-- the eligibility check UI can offer a typeahead without hitting the API on
-- every keystroke.  Rows are replaced wholesale on each admin-triggered sync.
--
-- insurance_plans.availity_payer_id is a soft link — NOT a FK constraint —
-- so rows in this table can be cleared and re-synced without cascading.

CREATE TABLE IF NOT EXISTS availity_payers (
    id               BIGSERIAL PRIMARY KEY,
    availity_id      TEXT        NOT NULL UNIQUE,  -- e.g. "BCBSM"
    payer_name       TEXT        NOT NULL,
    aliases_json     TEXT        NOT NULL DEFAULT '[]',  -- JSON array of strings
    transaction_types_json TEXT  NOT NULL DEFAULT '[]',  -- X12 transaction codes
    state_codes_json TEXT        NOT NULL DEFAULT '[]',  -- US state codes
    last_synced_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_availity_payers_name
    ON availity_payers (lower(payer_name));

-- Soft link from insurance_plans to availity_payers.availity_id.
-- NULL = not yet matched.  Nullable TEXT (not INT FK) so sync wipes don't break plans.
ALTER TABLE insurance_plans
    ADD COLUMN IF NOT EXISTS availity_payer_id TEXT;

CREATE INDEX IF NOT EXISTS ix_insurance_plans_availity_payer
    ON insurance_plans (availity_payer_id)
    WHERE availity_payer_id IS NOT NULL;
