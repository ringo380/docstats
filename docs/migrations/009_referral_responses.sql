-- Phase 1.D: referral_responses — closed-loop updates from the receiving side.
--
-- Multiple responses per referral (scheduled → completed progression).
-- Scope-transitive via parent referral (no scope columns). CASCADE on
-- parent hard-delete; soft-deleted referral hides responses via scope gate.
--
-- Apply via Supabase Management API:
--   curl -sS -X POST \
--     "https://api.supabase.com/v1/projects/uhnymifvdauzlmaogjfj/database/query" \
--     -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
--     -H "Content-Type: application/json" \
--     --data "$(jq -Rs '{query: .}' docs/migrations/009_referral_responses.sql)"

CREATE TABLE IF NOT EXISTS docstats_referral_responses (
    id                          BIGSERIAL PRIMARY KEY,
    referral_id                 INTEGER NOT NULL REFERENCES docstats_referrals(id) ON DELETE CASCADE,
    appointment_date            DATE,
    consult_completed           BOOLEAN NOT NULL DEFAULT FALSE,
    recommendations_text        TEXT,
    attached_consult_note_ref   TEXT,  -- reserved for Phase 10 file storage
    received_via                TEXT NOT NULL DEFAULT 'manual'
                                 CHECK (received_via IN ('fax','portal','email','phone','manual','api')),
    recorded_by_user_id         INTEGER REFERENCES docstats_users(id) ON DELETE SET NULL,
    created_at                  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

-- Timeline view on referral detail — newest response first.
CREATE INDEX IF NOT EXISTS idx_docstats_referral_responses_referral
    ON docstats_referral_responses (referral_id, created_at DESC, id DESC);

-- "Find all completed consults" admin query + closed-loop reporting.
CREATE INDEX IF NOT EXISTS idx_docstats_referral_responses_completed
    ON docstats_referral_responses (referral_id)
    WHERE consult_completed;
