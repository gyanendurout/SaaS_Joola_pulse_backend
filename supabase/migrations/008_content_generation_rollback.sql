-- ============================================================
-- ROLLBACK for 008_content_generation.sql
-- Run this ONLY if you need to undo migration 008.
-- Paste into Supabase SQL editor and click Run.
--
-- WARNING: this destroys ALL saved drafts + generation telemetry.
-- Safe to run even if 008 was only partially applied (uses IF EXISTS).
-- Does NOT touch any other existing tables.
-- ============================================================

-- 1. Drop trigger first (depends on content_drafts table)
drop trigger if exists content_drafts_updated_at on content_drafts;

-- 2. Drop the trigger function
drop function if exists _content_drafts_set_updated_at();

-- 3. Drop the 4 tables (CASCADE removes indexes + RLS policies automatically)
drop table if exists content_drafts             cascade;
drop table if exists content_generation_runs    cascade;
drop table if exists content_templates          cascade;
drop table if exists content_brand_voice        cascade;

-- After this runs, every table/index/policy/trigger/function created by
-- 008 is gone. You can re-run 008_content_generation.sql to recreate.
--
