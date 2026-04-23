-- Migration 016: share_tokens for external PHI-safe referral sharing (Phase 9.B)
--
-- A share_token is a single-use, expiring token tied to a delivery row.
-- The token plaintext is never stored — only its SHA-256 hash.
-- Recipients authenticate via a 2FA challenge (patient DOB or last-4 phone)
-- before PHI renders.

-- Postgres (Supabase)
CREATE TABLE IF NOT EXISTS docstats_share_tokens (
    id                      SERIAL PRIMARY KEY,
    delivery_id             INTEGER NOT NULL
                                REFERENCES docstats_deliveries(id) ON DELETE CASCADE,
    token_hash              TEXT NOT NULL UNIQUE,       -- SHA-256 of the URL-safe plaintext
    expires_at              TIMESTAMPTZ NOT NULL,
    revoked_at              TIMESTAMPTZ,
    second_factor_kind      TEXT NOT NULL DEFAULT 'none'
                                CHECK (second_factor_kind IN ('patient_dob', 'patient_phone_last4', 'none')),
    second_factor_hash      TEXT,                      -- HMAC-SHA256(SHARE_TOKEN_SECRET, answer)
    view_count              INTEGER NOT NULL DEFAULT 0,
    failed_attempts         INTEGER NOT NULL DEFAULT 0,
    last_viewed_at          TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_share_tokens_delivery_id
    ON docstats_share_tokens (delivery_id);
