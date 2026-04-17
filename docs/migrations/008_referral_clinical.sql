-- Phase 1.C: clinical sub-tables hanging off referrals.
--
-- Four tables: diagnoses, medications, allergies, attachments.
-- All CASCADE-delete with the parent referral (hard delete). All scope-
-- transitive through the referral (no scope columns on the sub-table rows).
--
-- Apply via Supabase Management API:
--   curl -sS -X POST \
--     "https://api.supabase.com/v1/projects/uhnymifvdauzlmaogjfj/database/query" \
--     -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
--     -H "Content-Type: application/json" \
--     --data "$(jq -Rs '{query: .}' docs/migrations/008_referral_clinical.sql)"

-- --- Diagnoses ---
CREATE TABLE IF NOT EXISTS docstats_referral_diagnoses (
    id           BIGSERIAL PRIMARY KEY,
    referral_id  INTEGER NOT NULL REFERENCES docstats_referrals(id) ON DELETE CASCADE,
    icd10_code   TEXT NOT NULL,
    icd10_desc   TEXT,
    is_primary   BOOLEAN NOT NULL DEFAULT FALSE,
    source       TEXT NOT NULL DEFAULT 'user_entered'
                  CHECK (source IN ('user_entered','imported_csv','nppes','ai_draft','carry_forward','ehr_import')),
    created_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_docstats_referral_diagnoses_referral
    ON docstats_referral_diagnoses (referral_id, is_primary DESC, id);

-- At most one primary diagnosis per referral.
CREATE UNIQUE INDEX IF NOT EXISTS idx_docstats_referral_diagnoses_one_primary
    ON docstats_referral_diagnoses (referral_id) WHERE is_primary;


-- --- Medications ---
CREATE TABLE IF NOT EXISTS docstats_referral_medications (
    id           BIGSERIAL PRIMARY KEY,
    referral_id  INTEGER NOT NULL REFERENCES docstats_referrals(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    dose         TEXT,
    route        TEXT,
    frequency    TEXT,
    source       TEXT NOT NULL DEFAULT 'user_entered'
                  CHECK (source IN ('user_entered','imported_csv','nppes','ai_draft','carry_forward','ehr_import')),
    created_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_docstats_referral_medications_referral
    ON docstats_referral_medications (referral_id, id);


-- --- Allergies ---
CREATE TABLE IF NOT EXISTS docstats_referral_allergies (
    id           BIGSERIAL PRIMARY KEY,
    referral_id  INTEGER NOT NULL REFERENCES docstats_referrals(id) ON DELETE CASCADE,
    substance    TEXT NOT NULL,
    reaction     TEXT,
    severity     TEXT,
    source       TEXT NOT NULL DEFAULT 'user_entered'
                  CHECK (source IN ('user_entered','imported_csv','nppes','ai_draft','carry_forward','ehr_import')),
    created_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_docstats_referral_allergies_referral
    ON docstats_referral_allergies (referral_id, id);


-- --- Attachments (checklist + future file storage) ---
CREATE TABLE IF NOT EXISTS docstats_referral_attachments (
    id                BIGSERIAL PRIMARY KEY,
    referral_id       INTEGER NOT NULL REFERENCES docstats_referrals(id) ON DELETE CASCADE,
    kind              TEXT NOT NULL
                       CHECK (kind IN ('lab','imaging','note','procedure','medication_list','problem_list','other')),
    label             TEXT NOT NULL,
    date_of_service   DATE,
    storage_ref       TEXT,  -- reserved for Phase 10 (Supabase Storage key)
    checklist_only    BOOLEAN NOT NULL DEFAULT TRUE,
    source            TEXT NOT NULL DEFAULT 'user_entered'
                       CHECK (source IN ('user_entered','imported_csv','nppes','ai_draft','carry_forward','ehr_import')),
    created_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_docstats_referral_attachments_referral
    ON docstats_referral_attachments (referral_id, id);
