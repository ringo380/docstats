-- Migration 027: prior-authorization / medical-necessity fields on referrals.
--
-- Phase context: AMA-style payer-facing letters (Scenario B in the AMA
-- referral-letter overhaul plan) need CPT/HCPCS service codes, place of
-- service, a medical-necessity narrative, and a step-therapy summary.
-- These are absent from the original referrals schema (which targeted
-- provider-to-provider consultation requests, not prior-auth packets).
--
-- All columns are nullable so existing referrals keep rendering through
-- the existing summary/scheduling/fax artifacts unchanged. The new
-- ``medical_necessity`` artifact only renders when payer info is
-- populated; the route layer gates the artifact picker on
-- ``referral.payer_plan_id IS NOT NULL OR referral.medical_necessity_text``.
--
-- ``cpt_codes`` is JSONB (Postgres) / JSON-as-TEXT (SQLite). Shape:
--   [
--     {"code": "99213", "description": "Office visit ...",
--      "units": 1, "modifier": null, "frequency": "once",
--      "duration": null}
--   ]
-- The route layer validates the shape; the DB enforces only that it
-- parses as valid JSON.
--
-- ``place_of_service_code`` follows CMS POS codes (2 digits, e.g. 11 =
-- office, 21 = inpatient hospital, 22 = on-campus outpatient hospital).
-- See https://www.cms.gov/Medicare/Coding/place-of-service-codes.

ALTER TABLE docstats_referrals
    ADD COLUMN IF NOT EXISTS cpt_codes JSONB,
    ADD COLUMN IF NOT EXISTS place_of_service_code TEXT,
    ADD COLUMN IF NOT EXISTS medical_necessity_text TEXT,
    ADD COLUMN IF NOT EXISTS conservative_therapy_tried TEXT,
    ADD COLUMN IF NOT EXISTS requested_start_date DATE,
    ADD COLUMN IF NOT EXISTS requested_end_date DATE;

-- POS codes are exactly 2 digits when present.
ALTER TABLE docstats_referrals
    ADD CONSTRAINT referrals_pos_code_format
    CHECK (place_of_service_code IS NULL OR place_of_service_code ~ '^[0-9]{2}$')
    NOT VALID;

-- start_date <= end_date when both are populated.
ALTER TABLE docstats_referrals
    ADD CONSTRAINT referrals_requested_date_range
    CHECK (
        requested_start_date IS NULL
        OR requested_end_date IS NULL
        OR requested_start_date <= requested_end_date
    )
    NOT VALID;
