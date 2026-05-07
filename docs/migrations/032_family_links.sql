-- Migration 032: Family links table
-- Tracks relationships between user accounts for adult family members
-- (spouse, adult children, etc.). Minor/dependent patient profiles are stored
-- as Patient rows under the parent's scope_user_id, not here.

CREATE TABLE IF NOT EXISTS docstats_family_links (
    id                 BIGSERIAL PRIMARY KEY,
    initiator_user_id  BIGINT NOT NULL REFERENCES docstats_users(id) ON DELETE CASCADE,
    linked_user_id     BIGINT NOT NULL REFERENCES docstats_users(id) ON DELETE CASCADE,
    relationship       TEXT NOT NULL,
    invite_token       TEXT UNIQUE,
    invite_email       TEXT,
    accepted_at        TIMESTAMPTZ,
    revoked_at         TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS docstats_family_links_pair_idx
    ON docstats_family_links (initiator_user_id, linked_user_id)
    WHERE revoked_at IS NULL;
