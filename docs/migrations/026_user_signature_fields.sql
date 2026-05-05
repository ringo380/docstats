-- Migration 026: signature-block fields on users.
--
-- Phase context: AMA-style referral letters require a proper signature
-- block (typed name, credentials, individual NPI, state license). The
-- existing users table only carries display_name + first/last/middle, so
-- letter renderers fall back to "—" for everything below the signature
-- line. These columns let the requesting clinician populate them once in
-- /profile and have every referral letter sign professionally.
--
-- All columns are nullable. No default values. Templates must render
-- gracefully when any field is absent (existing users keep working
-- without a profile update).
--
-- ``signature_image_ref`` stores an attachments-bucket object path
-- (PNG/JPEG, ~200KB ceiling enforced at upload time). The bucket is the
-- shared private Supabase Storage bucket; the object path is scoped to
-- the user (user-<id>/signature/<uuid>.png) per the existing
-- build_object_path convention in storage_files/base.py.

ALTER TABLE docstats_users
    ADD COLUMN IF NOT EXISTS credentials TEXT,
    ADD COLUMN IF NOT EXISTS individual_npi TEXT,
    ADD COLUMN IF NOT EXISTS state_license_number TEXT,
    ADD COLUMN IF NOT EXISTS state_license_state TEXT,
    ADD COLUMN IF NOT EXISTS signature_image_ref TEXT;

-- Soft validation: NPI is 10 digits when present.  Loose CHECK is
-- intentional — Luhn validation lives in domain code (validators.py)
-- which runs at the route boundary; the DB only enforces the obvious
-- format invariant so a free-text save can't slip a 9-digit value in.
ALTER TABLE docstats_users
    ADD CONSTRAINT users_individual_npi_format
    CHECK (individual_npi IS NULL OR individual_npi ~ '^[0-9]{10}$')
    NOT VALID;

-- 2-letter state code when present.
ALTER TABLE docstats_users
    ADD CONSTRAINT users_license_state_format
    CHECK (state_license_state IS NULL OR state_license_state ~ '^[A-Z]{2}$')
    NOT VALID;
