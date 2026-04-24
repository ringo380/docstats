-- Phase 10.C — per-org attachment retention window.
--
-- Default 2555 days (~7 years) matches the HIPAA §164.316(b)(2)(i) six-
-- year floor with a one-year safety margin. State requirements or legal
-- counsel may push orgs higher; the admin form surfaces this value.
--
-- Bounds: 30 days (floor — anything shorter risks purging documents before
-- initial delivery retries exhaust) and 10950 days (~30 years — above any
-- healthcare retention rule we've encountered).

ALTER TABLE docstats_organizations
  ADD COLUMN IF NOT EXISTS attachment_retention_days INTEGER
    NOT NULL
    DEFAULT 2555
    CHECK (attachment_retention_days BETWEEN 30 AND 10950);
