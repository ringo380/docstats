-- Migration 028: account_type + clinician verification on users.
--
-- Splits signups into patient vs clinician, with automated NPI/OIG
-- verification at signup time and a tri-state verdict (verified /
-- pending_review / rejected). Inconclusive signups still create the
-- user but are gated out of PHI features behind a banner until an
-- admin promotes — that admin queue is a follow-up PR.
--
-- Repurposes nothing: the dormant ``role_hint`` column added in the
-- Phase 0 ``_migrate_users_active_org_and_role_hint`` migration is
-- left in place but unused. A future cleanup migration can drop it.
--
-- All columns are nullable / defaulted so existing rows backfill
-- automatically (default everyone to ``patient``; the existing solo
-- account ``ringo380@gmail.com`` (id=6) can be flipped manually
-- post-deploy if desired).

ALTER TABLE docstats_users
    ADD COLUMN IF NOT EXISTS account_type TEXT NOT NULL DEFAULT 'patient',
    ADD COLUMN IF NOT EXISTS clinician_verification_status TEXT NOT NULL DEFAULT 'not_applicable',
    ADD COLUMN IF NOT EXISTS clinician_verified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS clinician_verified_method TEXT,
    ADD COLUMN IF NOT EXISTS clinician_verification_reasons JSONB;

ALTER TABLE docstats_users
    ADD CONSTRAINT users_account_type_check
    CHECK (account_type IN ('patient','clinician'))
    NOT VALID;

ALTER TABLE docstats_users
    ADD CONSTRAINT users_clinician_verification_status_check
    CHECK (clinician_verification_status IN
        ('not_applicable','verified','pending_review','rejected'))
    NOT VALID;
