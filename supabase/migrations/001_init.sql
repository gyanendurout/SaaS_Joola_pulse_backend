-- ============================================================
-- SEO Intel POC — Supabase schema (migration 001)
-- Run this in the Supabase SQL editor on a fresh project.
-- ============================================================

create extension if not exists "uuid-ossp";
create extension if not exists "pgcrypto";

-- ------------------------------------------------------------
-- runs : one row per "SEO project run" (URL + market + language)
-- ------------------------------------------------------------
create table if not exists runs (
  id                uuid primary key default gen_random_uuid(),
  website_url       text not null,
  canonical_domain  text,
  market            text not null default 'US',
  language          text not null default 'en',
  crawl_mode        text not null default 'full_site'
                    check (crawl_mode in ('full_site','sitemap_only','selected_urls','product_category_only')),
  max_pages         int  not null default 300,
  apify_enabled     boolean not null default false,

  status            text not null default 'pending'
                    check (status in ('pending','running','done','failed','cancelled')),
  current_agent     text,
  pages_crawled     int  not null default 0,
  pages_failed      int  not null default 0,
  error_message     text,

  started_at        timestamptz,
  finished_at       timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create index if not exists runs_status_idx     on runs(status);
create index if not exists runs_created_at_idx on runs(created_at desc);

-- ------------------------------------------------------------
-- pages : one row per crawled URL
-- ------------------------------------------------------------
create table if not exists pages (
  id                 uuid primary key default gen_random_uuid(),
  run_id             uuid not null references runs(id) on delete cascade,

  url                text not null,
  final_url          text,
  http_status        int,
  redirect_chain     jsonb default '[]'::jsonb,
  fetched_at         timestamptz not null default now(),
  fetcher            text not null check (fetcher in ('httpx','playwright','apify')),

  title              text,
  meta_description   text,
  h1                 text[] default '{}',
  h2                 text[] default '{}',
  h3                 text[] default '{}',
  canonical          text,
  robots_meta        text,
  is_indexable       boolean,
  word_count         int,
  text_content       text,

  internal_links     text[] default '{}',
  external_links     text[] default '{}',
  image_urls         text[] default '{}',
  images_missing_alt int  default 0,
  schema_types       text[] default '{}',
  schema_raw         jsonb default '[]'::jsonb,
  open_graph         jsonb default '{}'::jsonb,
  hreflang           jsonb default '[]'::jsonb,

  page_type          text,
  page_type_source   text default 'rule' check (page_type_source in ('rule','llm','manual')),

  content_hash       text,
  template_hint      text,

  html_storage_path  text,

  created_at         timestamptz not null default now()
);
create unique index if not exists pages_run_url_uniq on pages(run_id, url);
create index if not exists pages_run_idx       on pages(run_id);
create index if not exists pages_page_type_idx on pages(run_id, page_type);
create index if not exists pages_status_idx    on pages(run_id, http_status);
create index if not exists pages_hash_idx      on pages(run_id, content_hash);

-- ------------------------------------------------------------
-- issues : SEO issues per page (page_id null = site-wide)
-- ------------------------------------------------------------
create table if not exists issues (
  id              uuid primary key default gen_random_uuid(),
  run_id          uuid not null references runs(id) on delete cascade,
  page_id         uuid references pages(id) on delete cascade,
  issue_code      text not null,
  severity        text not null check (severity in ('critical','high','medium','low','info')),
  source          text not null default 'rule' check (source in ('rule','ai','api')),
  details         jsonb default '{}'::jsonb,
  recommendation  text,
  created_at      timestamptz not null default now()
);
create index if not exists issues_run_idx      on issues(run_id);
create index if not exists issues_page_idx     on issues(page_id);
create index if not exists issues_code_idx     on issues(run_id, issue_code);
create index if not exists issues_severity_idx on issues(run_id, severity);

-- ------------------------------------------------------------
-- entities : products / categories / services / brands / topics
-- ------------------------------------------------------------
create table if not exists entities (
  id              uuid primary key default gen_random_uuid(),
  run_id          uuid not null references runs(id) on delete cascade,
  entity_type     text not null check (entity_type in ('product','category','service','brand','topic','persona')),
  name            text not null,
  canonical_name  text not null,
  confidence      numeric(3,2) not null check (confidence between 0 and 1),
  attributes      jsonb default '{}'::jsonb,
  source_page_ids uuid[] default '{}',
  source          text not null default 'ai' check (source in ('rule','ai','mixed')),
  created_at      timestamptz not null default now()
);
create unique index if not exists entities_run_canonical_uniq on entities(run_id, entity_type, canonical_name);
create index if not exists entities_run_type_idx on entities(run_id, entity_type);

-- ------------------------------------------------------------
-- keywords : keyword research results
-- ------------------------------------------------------------
create table if not exists keywords (
  id                  uuid primary key default gen_random_uuid(),
  run_id              uuid not null references runs(id) on delete cascade,

  keyword             text not null,
  keyword_normalized  text not null,
  market              text not null,
  language            text not null,

  search_volume       int,
  cpc                 numeric(8,2),
  competition         numeric(3,2),
  keyword_difficulty  int,

  intent              text check (intent in ('informational','commercial','transactional','navigational','comparison','problem','local','brand')),
  keyword_type        text,

  seed_entity_id      uuid references entities(id) on delete set null,
  source              text not null check (source in ('dfs_keyword_ideas','dfs_related','dfs_suggestions','manual')),
  raw_payload         jsonb,

  suggested_page_id   uuid references pages(id) on delete set null,
  suggested_action    text check (suggested_action in ('optimize_existing','new_page','new_blog','new_category','none')),

  created_at          timestamptz not null default now()
);
create unique index if not exists keywords_run_kw_uniq on keywords(run_id, keyword_normalized, market, language);
create index if not exists keywords_run_idx     on keywords(run_id);
create index if not exists keywords_intent_idx  on keywords(run_id, intent);
create index if not exists keywords_volume_idx  on keywords(run_id, search_volume desc);

-- ------------------------------------------------------------
-- jobs : background job tracking
-- ------------------------------------------------------------
create table if not exists jobs (
  id           uuid primary key default gen_random_uuid(),
  run_id       uuid not null references runs(id) on delete cascade,
  job_type     text not null check (job_type in
               ('crawl','detect_issues','extract_entities','fetch_keywords','full_pipeline')),
  status       text not null default 'queued'
               check (status in ('queued','running','done','failed','cancelled')),
  progress     int  not null default 0,
  message      text,
  attempts     int  not null default 0,
  payload      jsonb default '{}'::jsonb,
  result       jsonb default '{}'::jsonb,
  error        text,
  started_at   timestamptz,
  finished_at  timestamptz,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index if not exists jobs_run_idx    on jobs(run_id);
create index if not exists jobs_status_idx on jobs(status);

-- ------------------------------------------------------------
-- updated_at triggers
-- ------------------------------------------------------------
create or replace function set_updated_at()
returns trigger as $$
begin new.updated_at = now(); return new; end;
$$ language plpgsql;

drop trigger if exists runs_updated_at on runs;
create trigger runs_updated_at before update on runs
  for each row execute function set_updated_at();

drop trigger if exists jobs_updated_at on jobs;
create trigger jobs_updated_at before update on jobs
  for each row execute function set_updated_at();
