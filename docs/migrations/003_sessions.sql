-- Phase 0.C: server-side session rows for remote revocation and audit.
-- Apply via Supabase Management API:
--   curl -sS -X POST \
--     "https://api.supabase.com/v1/projects/uhnymifvdauzlmaogjfj/database/query" \
--     -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
--     -H "Content-Type: application/json" \
--     --data "$(jq -Rs '{query: .}' docs/migrations/003_sessions.sql)"

CREATE TABLE IF NOT EXISTS docstats_sessions (
    id            TEXT PRIMARY KEY,
    user_id       INTEGER REFERENCES docstats_users(id) ON DELETE CASCADE,
    data          JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip            TEXT,
    user_agent    TEXT,
    created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    expires_at    TIMESTAMP WITH TIME ZONE NOT NULL,
    revoked_at    TIMESTAMP WITH TIME ZONE
);

-- Live-sessions-by-user — drives the "your active sessions" list.
CREATE INDEX IF NOT EXISTS idx_docstats_sessions_user
    ON docstats_sessions (user_id) WHERE revoked_at IS NULL;

-- Drives the purge-expired maintenance job.
CREATE INDEX IF NOT EXISTS idx_docstats_sessions_expires
    ON docstats_sessions (expires_at) WHERE revoked_at IS NULL;
