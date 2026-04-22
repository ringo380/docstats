-- Phase 7.D: org-level stale-referral threshold.
--
-- Controls the in-app /referrals banner for referrals that have sat in
-- awaiting_records or awaiting_auth past the configured number of days.
-- Solo users use the app default and do not read this column.
--
-- Apply via Supabase Management API:
--   curl -sS -X POST \
--     "https://api.supabase.com/v1/projects/uhnymifvdauzlmaogjfj/database/query" \
--     -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
--     -H "Content-Type: application/json" \
--     --data "$(jq -Rs '{query: .}' docs/migrations/013_organization_stale_threshold.sql)"

ALTER TABLE docstats_organizations
    ADD COLUMN IF NOT EXISTS stale_threshold_days INTEGER NOT NULL DEFAULT 3;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'docstats_organizations_stale_threshold_days_check'
    ) THEN
        ALTER TABLE docstats_organizations
            ADD CONSTRAINT docstats_organizations_stale_threshold_days_check
            CHECK (stale_threshold_days BETWEEN 1 AND 365);
    END IF;
END $$;
