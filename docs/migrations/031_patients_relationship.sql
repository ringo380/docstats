-- Migration 031: Add relationship column to docstats_patients
-- Stores the relationship label for dependent/family patient profiles
-- (e.g. "self", "child", "spouse"). NULL means not set.

ALTER TABLE docstats_patients ADD COLUMN relationship TEXT;
