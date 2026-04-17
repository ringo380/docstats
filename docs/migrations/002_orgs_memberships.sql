-- Phase 0.B: organizations + memberships + users.active_org_id + users.role_hint.
-- Also backfills the FK from docstats_audit_events.scope_organization_id now
-- that the target table exists.
--
-- Apply via Supabase Management API:
--   curl -sS -X POST \
--     "https://api.supabase.com/v1/projects/uhnymifvdauzlmaogjfj/database/query" \
--     -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
--     -H "Content-Type: application/json" \
--     --data "$(jq -Rs '{query: .}' docs/migrations/002_orgs_memberships.sql)"

CREATE TABLE IF NOT EXISTS docstats_organizations (
    id                    BIGSERIAL PRIMARY KEY,
    name                  TEXT NOT NULL,
    -- NOTE: slug uniqueness is enforced via the partial unique index below,
    -- NOT a column-level UNIQUE. A column-level UNIQUE would be unconditional
    -- and would block re-using a slug after soft-delete.
    slug                  TEXT NOT NULL,
    npi                   TEXT,
    address_line1         TEXT,
    address_line2         TEXT,
    address_city          TEXT,
    address_state         TEXT,
    address_zip           TEXT,
    phone                 TEXT,
    fax                   TEXT,
    terms_bundle_version  TEXT,
    created_at            TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    deleted_at            TIMESTAMP WITH TIME ZONE
);

-- Partial unique index: only live (non-deleted) slugs must be unique, so
-- soft-deleted orgs don't block re-use of the slug.
CREATE UNIQUE INDEX IF NOT EXISTS idx_docstats_organizations_live_slug
    ON docstats_organizations (slug) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS docstats_memberships (
    id                  BIGSERIAL PRIMARY KEY,
    organization_id     INTEGER NOT NULL REFERENCES docstats_organizations(id) ON DELETE CASCADE,
    user_id             INTEGER NOT NULL REFERENCES docstats_users(id) ON DELETE CASCADE,
    role                TEXT NOT NULL CHECK (role IN ('owner','admin','coordinator','clinician','staff','read_only')),
    invited_by_user_id  INTEGER REFERENCES docstats_users(id) ON DELETE SET NULL,
    joined_at           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMP WITH TIME ZONE,
    UNIQUE (organization_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_docstats_memberships_user
    ON docstats_memberships (user_id) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_docstats_memberships_org
    ON docstats_memberships (organization_id) WHERE deleted_at IS NULL;

ALTER TABLE docstats_users
    ADD COLUMN IF NOT EXISTS active_org_id INTEGER
        REFERENCES docstats_organizations(id) ON DELETE SET NULL;

ALTER TABLE docstats_users
    ADD COLUMN IF NOT EXISTS role_hint TEXT;

-- Add the deferred FK on audit_events.scope_organization_id now that the
-- target table exists. Idempotent via pg_constraint check.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'docstats_audit_events_scope_org_fkey'
    ) THEN
        ALTER TABLE docstats_audit_events
            ADD CONSTRAINT docstats_audit_events_scope_org_fkey
            FOREIGN KEY (scope_organization_id)
            REFERENCES docstats_organizations(id)
            ON DELETE SET NULL;
    END IF;
END $$;
