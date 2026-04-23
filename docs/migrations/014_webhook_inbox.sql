-- Phase 8.C: dead-lettered inbound webhook inbox.
--
-- Stores every HMAC-verified POST to /api/v2/webhooks/inbound for future
-- triage. No routing / handlers yet — Phase 9+ will consume these rows as
-- delivery-status callbacks, EHR pushes, etc. land.
--
-- Apply via Supabase Management API:
--   curl -sS -X POST \
--     "https://api.supabase.com/v1/projects/uhnymifvdauzlmaogjfj/database/query" \
--     -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
--     -H "Content-Type: application/json" \
--     --data "$(jq -Rs '{query: .}' docs/migrations/014_webhook_inbox.sql)"

CREATE TABLE IF NOT EXISTS docstats_webhook_inbox (
    id                  BIGSERIAL PRIMARY KEY,
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    source              TEXT,
    payload_json        JSONB NOT NULL,
    http_headers_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
    signature           TEXT,
    status              TEXT NOT NULL DEFAULT 'received'
                            CHECK (status IN ('received', 'processed', 'discarded', 'invalid_signature')),
    notes               TEXT,
    processed_at        TIMESTAMPTZ,
    CONSTRAINT docstats_webhook_inbox_payload_size_check
        CHECK (octet_length(payload_json::text) <= 262144)
);

-- Support purge sweeps (Phase 9) — "old received rows we can safely drop".
CREATE INDEX IF NOT EXISTS idx_docstats_webhook_inbox_status_received_at
    ON docstats_webhook_inbox (status, received_at);
