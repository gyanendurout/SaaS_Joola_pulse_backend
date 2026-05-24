-- ============================================================
-- Migration 006 — News Intelligence tables
-- Idempotent: safe to run multiple times.
-- Paste into Supabase SQL editor and click Run.
-- ============================================================

-- ------------------------------------------------------------
-- news_scrape_runs : one row per scrape job
-- ------------------------------------------------------------
create table if not exists news_scrape_runs (
  id                      uuid primary key default gen_random_uuid(),
  status                  text not null default 'pending',
  run_type                text default 'manual',
  lookback_days           int  default 180,
  sites_total             int  not null default 0,
  sites_scraped           int  not null default 0,
  articles_found          int  not null default 0,
  articles_new            int  not null default 0,
  articles_with_mentions  int  not null default 0,
  joola_related_articles  int  default 0,
  successful_sources      int  default 0,
  failed_sources          int  default 0,
  error_message           text,
  started_at              timestamptz,
  finished_at             timestamptz,
  created_at              timestamptz not null default now()
);
create index if not exists news_scrape_runs_created_idx on news_scrape_runs(created_at desc);

-- Add any missing columns to existing news_scrape_runs table
alter table news_scrape_runs add column if not exists run_type               text    default 'manual';
alter table news_scrape_runs add column if not exists lookback_days          int     default 180;
alter table news_scrape_runs add column if not exists joola_related_articles int     default 0;
alter table news_scrape_runs add column if not exists successful_sources     int     default 0;
alter table news_scrape_runs add column if not exists failed_sources         int     default 0;
alter table news_scrape_runs add column if not exists error_message          text;

-- ------------------------------------------------------------
-- news_articles : one row per article (unique on url)
-- ------------------------------------------------------------
create table if not exists news_articles (
  id                    uuid primary key default gen_random_uuid(),
  url                   text not null unique,
  source_site           text not null,
  title                 text not null,
  excerpt               text default '',
  content_text          text default '',
  author                text default '',
  image_url             text default '',
  published_at          timestamptz,
  scraped_at            timestamptz,
  is_active             boolean not null default true,

  -- Entity detection
  is_joola_mention      boolean not null default false,
  joola_context         text    default '',
  players_mentioned     text[]  default '{}',
  competitors_mentioned text[]  default '{}',
  has_competitor_mention boolean not null default false,

  -- Classification
  sentiment             text    default 'informative',
  sentiment_score       numeric(4,2) default 0,
  article_type          text    default 'general',
  relevance_type        text,
  importance_score      numeric(5,1) default 0,
  suggested_action      text    default 'No action needed',

  -- AI enrichment
  ai_summary            text,
  why_it_matters        text,

  -- Dedup
  content_hash          varchar(64),
  word_count            int     default 0,

  created_at            timestamptz not null default now()
);
create index if not exists news_articles_published_idx  on news_articles(published_at desc);
create index if not exists news_articles_source_idx     on news_articles(source_site);
create index if not exists news_articles_joola_idx      on news_articles(is_joola_mention);
create index if not exists news_articles_sentiment_idx  on news_articles(sentiment);
create index if not exists news_articles_importance_idx on news_articles(importance_score desc);
create index if not exists news_articles_active_idx     on news_articles(is_active, published_at desc);

-- Add any missing columns to existing news_articles table
alter table news_articles add column if not exists content_text           text    default '';
alter table news_articles add column if not exists competitors_mentioned  text[]  default '{}';
alter table news_articles add column if not exists has_competitor_mention boolean default false;
alter table news_articles add column if not exists relevance_type         text;
alter table news_articles add column if not exists importance_score       numeric(5,1) default 0;
alter table news_articles add column if not exists suggested_action       text    default 'No action needed';
alter table news_articles add column if not exists ai_summary             text;
alter table news_articles add column if not exists why_it_matters         text;
alter table news_articles add column if not exists content_hash           varchar(64);
alter table news_articles add column if not exists word_count             int     default 0;
alter table news_articles add column if not exists is_active              boolean default true;

-- Drop old strict sentiment check if it exists, then add updated one
alter table news_articles drop constraint if exists news_articles_sentiment_check;
alter table news_articles add constraint news_articles_sentiment_check
  check (sentiment in ('positive','negative','informative','neutral','mixed','risk'));

-- ------------------------------------------------------------
-- news_sources : health + authority per site
-- ------------------------------------------------------------
create table if not exists news_sources (
  id             uuid primary key default gen_random_uuid(),
  name           text not null unique,
  base_url       text not null,
  authority_score int  not null default 50,
  is_active      boolean not null default true,
  last_success_at timestamptz,
  last_failed_at  timestamptz,
  last_error      text,
  total_articles  int  default 0,
  created_at      timestamptz not null default now()
);

-- Seed the 20 tracked sites (upsert so safe to re-run)
insert into news_sources (name, base_url, authority_score) values
  ('pickleball.com',              'https://pickleball.com',               80),
  ('thedinkpickleball.com',       'https://www.thedinkpickleball.com',    75),
  ('pickleballunion.com',         'https://pickleballunion.com',          60),
  ('pickleballmagazine.com',      'https://www.pickleballmagazine.com',   70),
  ('usapickleball.org',           'https://usapickleball.org',            90),
  ('ppatour.com',                 'https://www.ppatour.com',              85),
  ('majorleaguepickleball.co',    'https://majorleaguepickleball.co',     85),
  ('theapp.global',               'https://www.theapp.global',            80),
  ('dupr.com',                    'https://www.dupr.com',                 75),
  ('pickleheads.com',             'https://www.pickleheads.com',          65),
  ('pickleballcentral.com',       'https://pickleballcentral.com',        60),
  ('justpaddles.com',             'https://www.justpaddles.com',          60),
  ('thekitchenpickle.com',        'https://thekitchenpickle.com',         65),
  ('pickleballportal.com',        'https://www.pickleballportal.com',     60),
  ('pickleballstudio.com',        'https://pickleballstudio.com',         55),
  ('pickleballeffect.com',        'https://pickleballeffect.com',         55),
  ('selkirk.com',                 'https://www.selkirk.com',              70),
  ('worldpickleballmagazine.com', 'https://worldpickleballmagazine.com',  65),
  ('pickleballnewsasia.com',      'https://pickleballnewsasia.com',       50),
  ('pickleballtoday.co',          'https://pickleballtoday.co',           55)
on conflict (name) do update
  set base_url       = excluded.base_url,
      authority_score = excluded.authority_score;

-- ------------------------------------------------------------
-- news_scrape_errors : per-site error log
-- ------------------------------------------------------------
create table if not exists news_scrape_errors (
  id             uuid primary key default gen_random_uuid(),
  scrape_run_id  uuid references news_scrape_runs(id) on delete cascade,
  source_name    text not null,
  url            text default '',
  error_type     text default 'scrape_error',
  error_message  text,
  status_code    int,
  created_at     timestamptz not null default now()
);
create index if not exists news_scrape_errors_run_idx on news_scrape_errors(scrape_run_id);

-- Force PostgREST to reload schema cache
notify pgrst, 'reload schema';
