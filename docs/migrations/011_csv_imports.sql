-- Phase 1.F: CSV bulk import staging tables.
--
-- Two tables: csv_imports (batch-level) and csv_import_rows (per-row).
-- csv_imports is scope-owned (user XOR org). csv_import_rows is scope-
-- transitive via parent import (no scope columns, CASCADE on parent).
--
-- Apply via Supabase Management API:
--   curl -sS -X POST \
--     "https://api.supabase.com/v1/projects/uhnymifvdauzlmaogjfj/database/query" \
--     -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
--     -H "Content-Type: application/json" \
--     --data "$(jq -Rs '{query: .}' docs/migrations/011_csv_imports.sql)"

CREATE TABLE IF NOT EXISTS docstats_csv_imports (
    id                       BIGSERIAL PRIMARY KEY,
    scope_user_id            INTEGER REFERENCES docstats_users(id) ON DELETE CASCADE,
    scope_organization_id    INTEGER REFERENCES docstats_organizations(id) ON DELETE CASCADE,

    uploaded_by_user_id      INTEGER REFERENCES docstats_users(id) ON DELETE SET NULL,
    original_filename        TEXT NOT NULL,
    row_count                INTEGER NOT NULL DEFAULT 0,
    status                   TEXT NOT NULL DEFAULT 'uploaded'
                              CHECK (status IN ('uploaded','mapped','validated','committed','failed')),

    mapping                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_report             JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at               TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at               TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),

    CONSTRAINT docstats_csv_imports_scope_exactly_one CHECK (
        (scope_user_id IS NOT NULL AND scope_organization_id IS NULL)
        OR (scope_user_id IS NULL AND scope_organization_id IS NOT NULL)
    )
);

-- Recent-imports list views.
CREATE INDEX IF NOT EXISTS idx_docstats_csv_imports_scope_user
    ON docstats_csv_imports (scope_user_id, created_at DESC)
    WHERE scope_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_docstats_csv_imports_scope_org
    ON docstats_csv_imports (scope_organization_id, created_at DESC)
    WHERE scope_organization_id IS NOT NULL;


CREATE TABLE IF NOT EXISTS docstats_csv_import_rows (
    id                  BIGSERIAL PRIMARY KEY,
    import_id           INTEGER NOT NULL REFERENCES docstats_csv_imports(id) ON DELETE CASCADE,
    row_index           INTEGER NOT NULL,
    raw_json            JSONB NOT NULL DEFAULT '{}'::jsonb,
    validation_errors   JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Set when a row is committed as a referral. ON DELETE SET NULL so
    -- soft-deleting / hard-deleting a created referral doesn't wipe the
    -- provenance trail on the import row.
    referral_id         INTEGER REFERENCES docstats_referrals(id) ON DELETE SET NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending','valid','error','committed','skipped')),
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

-- Review-page ordering: rows shown in file order.
CREATE INDEX IF NOT EXISTS idx_docstats_csv_import_rows_import
    ON docstats_csv_import_rows (import_id, row_index);

-- "Show me only the error rows" filter.
CREATE INDEX IF NOT EXISTS idx_docstats_csv_import_rows_status
    ON docstats_csv_import_rows (import_id, status)
    WHERE status IN ('error','pending');

-- Only one row per (import, row_index) — guards against accidental
-- double-inserts during upload parsing.
CREATE UNIQUE INDEX IF NOT EXISTS idx_docstats_csv_import_rows_unique_index
    ON docstats_csv_import_rows (import_id, row_index);
