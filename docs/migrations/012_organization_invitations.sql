-- Phase 6.F: organization_invitations table.
--
-- Admins generate a signed magic link for a specific email + role. The
-- invitee clicks the link, logs in / signs up (email must match), and
-- accepts — which triggers a new docstats_memberships row.
--
-- Token is stored plaintext because:
--   - single-use (once accepted, accepted_at is set and the row can't
--     be reused),
--   - short-lived (default 7 days, bounded 1h–90d),
--   - possessing the token grants membership, not PHI/passwords.
--
-- Apply via Supabase Management API:
--   curl -sS -X POST \
--     "https://api.supabase.com/v1/projects/uhnymifvdauzlmaogjfj/database/query" \
--     -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
--     -H "Content-Type: application/json" \
--     --data "$(jq -Rs '{query: .}' docs/migrations/012_organization_invitations.sql)"

CREATE TABLE IF NOT EXISTS docstats_organization_invitations (
    id                   BIGSERIAL PRIMARY KEY,
    organization_id      INTEGER NOT NULL REFERENCES docstats_organizations(id) ON DELETE CASCADE,
    email                TEXT NOT NULL,
    role                 TEXT NOT NULL
        CHECK (role IN ('owner','admin','coordinator','clinician','staff','read_only')),
    token                TEXT NOT NULL UNIQUE,
    invited_by_user_id   INTEGER REFERENCES docstats_users(id) ON DELETE SET NULL,
    expires_at           TIMESTAMP WITH TIME ZONE NOT NULL,
    accepted_at          TIMESTAMP WITH TIME ZONE,
    revoked_at           TIMESTAMP WITH TIME ZONE,
    created_at           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_docstats_invitations_org
    ON docstats_organization_invitations (organization_id, created_at DESC);

-- Partial unique index: at most one LIVE (pending + not-yet-expired)
-- invitation per (organization_id, email). Accepted or revoked rows
-- don't block re-invitation.
CREATE UNIQUE INDEX IF NOT EXISTS idx_docstats_invitations_pending_unique
    ON docstats_organization_invitations (organization_id, email)
    WHERE accepted_at IS NULL AND revoked_at IS NULL;
