-- Phase 0 fix: drop the redundant column-level UNIQUE on docstats_organizations.slug.
--
-- Migration 002 declared `slug TEXT NOT NULL UNIQUE` at the column level AND
-- `CREATE UNIQUE INDEX ... WHERE deleted_at IS NULL` as a partial index. The
-- column-level UNIQUE is unconditional, so soft-deleted slugs still blocked
-- reuse — contradicting the partial-index intent. The column-level constraint
-- must go; only the partial index should remain.
--
-- Apply via Supabase Management API:
--   curl -sS -X POST \
--     "https://api.supabase.com/v1/projects/uhnymifvdauzlmaogjfj/database/query" \
--     -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
--     -H "Content-Type: application/json" \
--     --data "$(jq -Rs '{query: .}' docs/migrations/005_fix_slug_unique.sql)"

DO $$
DECLARE
    constraint_name TEXT;
BEGIN
    -- Find the system-generated UNIQUE constraint name for the slug column.
    SELECT conname INTO constraint_name
    FROM pg_constraint
    WHERE conrelid = 'docstats_organizations'::regclass
      AND contype = 'u'
      AND array_length(conkey, 1) = 1
      AND conkey[1] = (
          SELECT attnum FROM pg_attribute
          WHERE attrelid = 'docstats_organizations'::regclass
            AND attname = 'slug'
      );

    IF constraint_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE docstats_organizations DROP CONSTRAINT %I',
            constraint_name
        );
    END IF;
END $$;
