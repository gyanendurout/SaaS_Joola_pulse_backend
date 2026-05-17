-- Allow per-run seed keyword overrides.
-- Original auto-generated seed keywords come from the entities/LLM step;
-- the user can edit, add or remove them via the dashboard. This column
-- stores the user-curated list (NULL = use the auto-generated list).

alter table runs
  add column if not exists seed_keywords text[] default null;
