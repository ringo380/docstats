-- Phase 0.A: append-only audit event log.
-- Apply via Supabase Management API SQL endpoint:
--   POST https://api.supabase.com/v1/projects/{ref}/database/query
--   Authorization: Bearer $SUPABASE_ACCESS_TOKEN
--   body: {"query": "<contents of this file>"}
--
-- Tables are prefixed with docstats_ to coexist with other apps in the
-- shared Supabase project (ref: uhnymifvdauzlmaogjfj).

CREATE TABLE IF NOT EXISTS docstats_audit_events (
    id                    BIGSERIAL PRIMARY KEY,
    actor_user_id         INTEGER REFERENCES docstats_users(id) ON DELETE SET NULL,
    scope_user_id         INTEGER REFERENCES docstats_users(id) ON DELETE SET NULL,
    scope_organization_id INTEGER,
    action                TEXT NOT NULL,
    entity_type           TEXT,
    entity_id             TEXT,
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip                    TEXT,
    user_agent            TEXT,
    created_at            TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_docstats_audit_events_actor
    ON docstats_audit_events (actor_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_docstats_audit_events_scope_user
    ON docstats_audit_events (scope_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_docstats_audit_events_scope_org
    ON docstats_audit_events (scope_organization_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_docstats_audit_events_entity
    ON docstats_audit_events (entity_type, entity_id, created_at DESC);
