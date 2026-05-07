-- Migration 033: Org-scoped EHR connections (Phase 12.E Redox aggregator).
--
-- Two changes in one migration:
--
-- 1. Widen the ehr_vendor CHECK to include 'ecw_smart' and 'redox'.
--    NOTE: 'ecw_smart' was added to docstats.domain.ehr.EHR_VENDORS during
--    Phase 12.D (commit 43d7589) but the corresponding Postgres CHECK widening
--    never landed as a separate migration file. We fold that fix-up into this
--    migration so prod CHECK matches the Python-side vendor list.
--
-- 2. Allow EHR connections to be owned by an organization instead of a user.
--    This is required for the Redox aggregator integration where authentication
--    is backend-to-backend (one shared OAuth API key per org tenant), not
--    per-user SMART-on-FHIR. We add organization_id as a nullable column and
--    enforce exactly-one-owner via a CHECK constraint, then add a partial
--    UNIQUE index so each org has at most one active connection per vendor.

-- ---- 1) Widen vendor CHECK constraint ------------------------------------
ALTER TABLE docstats_ehr_connections
    DROP CONSTRAINT IF EXISTS docstats_ehr_connections_ehr_vendor_check;

ALTER TABLE docstats_ehr_connections
    ADD CONSTRAINT docstats_ehr_connections_ehr_vendor_check
    CHECK (ehr_vendor IN ('epic_sandbox', 'cerner_oauth', 'ecw_smart', 'redox'));

-- ---- 2) Add organization_id + relax user_id NOT NULL ---------------------
ALTER TABLE docstats_ehr_connections
    ADD COLUMN IF NOT EXISTS organization_id INTEGER
        REFERENCES docstats_organizations(id) ON DELETE CASCADE;

ALTER TABLE docstats_ehr_connections
    ALTER COLUMN user_id DROP NOT NULL;

-- ---- 3) Exactly-one-owner CHECK ------------------------------------------
ALTER TABLE docstats_ehr_connections
    DROP CONSTRAINT IF EXISTS docstats_ehr_connections_owner_check;

ALTER TABLE docstats_ehr_connections
    ADD CONSTRAINT docstats_ehr_connections_owner_check
    CHECK (
        (user_id IS NOT NULL AND organization_id IS NULL)
        OR (user_id IS NULL AND organization_id IS NOT NULL)
    );

-- ---- 3b) Relax NOT NULL on token-related columns -------------------------
-- Redox connections use JWT-bearer assertion (RFC 7523): we sign a fresh
-- short-lived JWT for each token request and don't persist Bearer tokens.
-- Loosening these to NULL avoids a sentinel-string lie in the row.
-- Per-vendor enforcement (Epic/Cerner/eCW still require these) lives at the
-- Python storage-method layer.
ALTER TABLE docstats_ehr_connections
    ALTER COLUMN access_token_enc DROP NOT NULL;

ALTER TABLE docstats_ehr_connections
    ALTER COLUMN expires_at DROP NOT NULL;

ALTER TABLE docstats_ehr_connections
    ALTER COLUMN scope DROP NOT NULL;

-- ---- 4) Partial UNIQUE index on (organization_id, ehr_vendor) ------------
-- Mirrors the existing user-scoped partial unique index. Only applies when
-- organization_id is set; the existing user-vendor index keeps user-scoped
-- rows unique on its side.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ehr_connections_org_active
    ON docstats_ehr_connections (organization_id, ehr_vendor)
    WHERE revoked_at IS NULL AND organization_id IS NOT NULL;
