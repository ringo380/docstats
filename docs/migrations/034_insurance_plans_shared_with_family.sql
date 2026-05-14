-- Migration 034: Allow user-scoped insurance plans to be shared with family.
--
-- When a primary insurance holder marks a plan as shared, any user linked to
-- them via an active family_links row sees the plan (read-only) in their own
-- referral plan picker. Only meaningful when scope_user_id IS NOT NULL;
-- org-scoped plans ignore the flag (org members already share visibility).
--
-- Backs GitHub issue #159.

ALTER TABLE docstats_insurance_plans
    ADD COLUMN IF NOT EXISTS shared_with_family BOOLEAN NOT NULL DEFAULT FALSE;
