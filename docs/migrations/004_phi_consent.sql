-- Phase 0.D: PHI-specific consent tracking, separate from the general ToS.
--
-- `terms_accepted_at` / `terms_version` track acceptance of the general
-- Terms of Service + Privacy Policy — an every-user gate at onboarding.
--
-- `phi_consent_*` is a SECOND, finer-grained consent track that gates the
-- referral platform's PHI-entry surface (Phase 2+): diagnoses, medications,
-- allergies, insurance, auth numbers, patient demographics distinct from the
-- account holder. We version it separately so PHI scope changes don't force
-- every user to re-accept the general ToS.
--
-- Apply via Supabase Management API:
--   curl -sS -X POST \
--     "https://api.supabase.com/v1/projects/uhnymifvdauzlmaogjfj/database/query" \
--     -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
--     -H "Content-Type: application/json" \
--     --data "$(jq -Rs '{query: .}' docs/migrations/004_phi_consent.sql)"

ALTER TABLE docstats_users
    ADD COLUMN IF NOT EXISTS phi_consent_version TEXT;

ALTER TABLE docstats_users
    ADD COLUMN IF NOT EXISTS phi_consent_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE docstats_users
    ADD COLUMN IF NOT EXISTS phi_consent_ip TEXT;

ALTER TABLE docstats_users
    ADD COLUMN IF NOT EXISTS phi_consent_user_agent TEXT;
