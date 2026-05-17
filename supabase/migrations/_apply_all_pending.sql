-- ============================================================
-- Apply all pending migrations (002, 003, 004) in one go.
-- Idempotent — safe to run multiple times.
-- Paste into Supabase SQL editor and click Run.
-- ============================================================

-- ── 002: allow 'single_url' crawl mode ─────────────────────────
alter table runs drop constraint if exists runs_crawl_mode_check;
alter table runs add constraint runs_crawl_mode_check
  check (crawl_mode in (
    'full_site', 'sitemap_only', 'selected_urls',
    'product_category_only', 'single_url'
  ));

-- ── 003: extended tables for SERP, backlinks, competitors,
--           ranked keywords, gap analyses + recommendations JSON ──
alter table runs add column if not exists previous_run_id uuid references runs(id);
alter table runs add column if not exists recommendations jsonb;

create table if not exists serp_results (
  id                 uuid primary key default gen_random_uuid(),
  run_id             uuid not null references runs(id) on delete cascade,
  keyword            text not null,
  search_volume      int,
  our_rank           int,
  our_url            text,
  organic            jsonb default '[]'::jsonb,
  people_also_ask    text[] default '{}',
  related_searches   text[] default '{}',
  created_at         timestamptz not null default now()
);
create index if not exists serp_results_run_idx on serp_results(run_id);

create table if not exists backlinks_summary (
  id                          uuid primary key default gen_random_uuid(),
  run_id                      uuid not null references runs(id) on delete cascade,
  domain                      text not null,
  total_backlinks             int,
  total_referring_domains     int,
  total_referring_ips         int,
  domain_rank                 int,
  broken_backlinks            int,
  referring_domains_nofollow  int,
  raw_data                    jsonb,
  created_at                  timestamptz not null default now()
);
create index if not exists backlinks_summary_run_idx on backlinks_summary(run_id);

create table if not exists competitor_domains (
  id                   uuid primary key default gen_random_uuid(),
  run_id               uuid not null references runs(id) on delete cascade,
  domain               text not null,
  avg_position         numeric(8,2),
  sum_position         bigint,
  intersections        int,
  full_domain_metrics  jsonb,
  created_at           timestamptz not null default now()
);
create index if not exists competitor_domains_run_idx on competitor_domains(run_id);

create table if not exists domain_ranked_keywords (
  id                  uuid primary key default gen_random_uuid(),
  run_id              uuid not null references runs(id) on delete cascade,
  keyword             text not null,
  keyword_normalized  text not null,
  position            int,
  url                 text,
  search_volume       int,
  cpc                 numeric(8,2),
  traffic             numeric(10,2),
  created_at          timestamptz not null default now()
);
create index if not exists domain_ranked_kw_run_idx on domain_ranked_keywords(run_id);
create index if not exists domain_ranked_kw_pos_idx on domain_ranked_keywords(run_id, position);

create table if not exists gap_analyses (
  id                      uuid primary key default gen_random_uuid(),
  run_id                  uuid not null references runs(id) on delete cascade,
  previous_run_id         uuid references runs(id),
  summary                 text,
  new_issues              jsonb default '[]'::jsonb,
  fixed_issues            jsonb default '[]'::jsonb,
  new_ranked_keywords     jsonb default '[]'::jsonb,
  lost_ranked_keywords    jsonb default '[]'::jsonb,
  rank_improvements       jsonb default '[]'::jsonb,
  rank_declines           jsonb default '[]'::jsonb,
  keyword_volume_gained   int  default 0,
  created_at              timestamptz not null default now()
);
create index if not exists gap_analyses_run_idx on gap_analyses(run_id);

-- ── 004: user-curated seed keywords on the run ────────────────
alter table runs
  add column if not exists seed_keywords text[] default null;

-- ── Force PostgREST to reload schema cache so the new tables /
--    columns are immediately visible to the API
notify pgrst, 'reload schema';
