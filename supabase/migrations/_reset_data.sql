-- Reset all SEO Intel run data.
-- Paste into the Supabase SQL Editor and run.
-- Truncating `runs` cascades to every child table.

truncate table runs cascade;
truncate table jobs cascade;
