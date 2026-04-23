-- Phase 9.A: outbound delivery scaffolding.
--
-- Adds `docstats_deliveries` (the actual delivery attempts) and
-- `docstats_delivery_attempts` (per-retry history). Extends the
-- referral_events CHECK constraint to allow the three new delivery
-- event types: `dispatched`, `delivered`, `delivery_failed`.
--
-- The CHECK constraint bump uses the NOT VALID + VALIDATE pattern so
-- an in-flight deploy with existing rows doesn't block on validation.
-- Existing rows are guaranteed to satisfy the new constraint (the new
-- values are additive) so `VALIDATE CONSTRAINT` will succeed.
--
-- Apply via Supabase Management API:
--   curl -sS -X POST \
--     "https://api.supabase.com/v1/projects/uhnymifvdauzlmaogjfj/database/query" \
--     -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
--     -H "Content-Type: application/json" \
--     --data "$(jq -Rs '{query: .}' docs/migrations/015_delivery_tables.sql)"

BEGIN;

-- Step 1: bump the referral_events event_type CHECK.
-- Drop old + add new in one transaction. Existing data is
-- guaranteed compatible (the set of allowed values is strictly
-- extended) so NOT VALID / VALIDATE isn't strictly required,
-- but we use it for future-proofing when the constraint may drift.
ALTER TABLE docstats_referral_events
    DROP CONSTRAINT IF EXISTS docstats_referral_events_event_type_check;

ALTER TABLE docstats_referral_events
    ADD CONSTRAINT docstats_referral_events_event_type_check
    CHECK (event_type IN (
        'created',
        'status_changed',
        'field_edited',
        'exported',
        'sent',
        'response_received',
        'note_added',
        'assigned',
        'unassigned',
        'dispatched',
        'delivered',
        'delivery_failed'
    )) NOT VALID;

ALTER TABLE docstats_referral_events
    VALIDATE CONSTRAINT docstats_referral_events_event_type_check;

-- Step 2: deliveries table.
CREATE TABLE IF NOT EXISTS docstats_deliveries (
    id                      BIGSERIAL PRIMARY KEY,
    referral_id             BIGINT NOT NULL
                                REFERENCES docstats_referrals(id) ON DELETE CASCADE,
    -- Scope columns denormalized from parent referral (admin list queries
    -- avoid a join). Exactly one must be non-NULL — enforced via CHECK.
    scope_user_id           BIGINT REFERENCES docstats_users(id) ON DELETE SET NULL,
    scope_organization_id   BIGINT REFERENCES docstats_organizations(id) ON DELETE SET NULL,
    channel                 TEXT NOT NULL CHECK (channel IN ('fax', 'email', 'direct')),
    recipient               TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'queued'
                                CHECK (status IN ('queued', 'sending', 'sent', 'delivered', 'failed', 'cancelled')),
    vendor_name             TEXT,
    vendor_message_id       TEXT,
    -- Dedup key for vendor webhook replays. Set by the channel impl at
    -- enqueue time, typically `<channel>:<uuid>`. Enforced unique via
    -- partial index below.
    idempotency_key         TEXT,
    -- Packet composition spec — `{"include": ["fax_cover", "summary"]}`.
    -- JSON so the shape can extend without a migration.
    packet_artifact         JSONB NOT NULL DEFAULT '{}'::jsonb,
    retry_count             INTEGER NOT NULL DEFAULT 0,
    last_error_code         TEXT,
    last_error_message      TEXT,
    cancelled_at            TIMESTAMPTZ,
    cancelled_by_user_id    BIGINT REFERENCES docstats_users(id) ON DELETE SET NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at                 TIMESTAMPTZ,
    delivered_at            TIMESTAMPTZ,
    CONSTRAINT docstats_deliveries_scope_exactly_one
        CHECK (
            (scope_user_id IS NOT NULL AND scope_organization_id IS NULL)
         OR (scope_user_id IS NULL AND scope_organization_id IS NOT NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_docstats_deliveries_referral
    ON docstats_deliveries(referral_id);

-- Sweeper query: grab rows in queued/sending older than threshold.
CREATE INDEX IF NOT EXISTS idx_docstats_deliveries_status_created
    ON docstats_deliveries(status, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_docstats_deliveries_idempotency
    ON docstats_deliveries(idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Step 3: delivery_attempts table.
-- Per-attempt history for operator triage. retry_count on the parent
-- tells you "how many" but not "why" — this table tells you why.
CREATE TABLE IF NOT EXISTS docstats_delivery_attempts (
    id                      BIGSERIAL PRIMARY KEY,
    delivery_id             BIGINT NOT NULL
                                REFERENCES docstats_deliveries(id) ON DELETE CASCADE,
    attempt_number          INTEGER NOT NULL,
    started_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at            TIMESTAMPTZ,
    result                  TEXT NOT NULL DEFAULT 'in_progress'
                                CHECK (result IN ('in_progress', 'success', 'retryable', 'fatal')),
    error_code              TEXT,
    error_message           TEXT,  -- truncated to 500 chars at write time
    vendor_response_excerpt TEXT   -- truncated to 2000 chars at write time
);

CREATE INDEX IF NOT EXISTS idx_docstats_delivery_attempts_delivery
    ON docstats_delivery_attempts(delivery_id, attempt_number);

COMMIT;
