-- 005: Content generation, calendar, integrations, performance cache

-- Generated content (blog ideas, outlines, drafts, emails, social posts)
create table if not exists generated_content (
  id           uuid primary key default gen_random_uuid(),
  run_id       uuid not null references runs(id) on delete cascade,
  content_type text not null,   -- 'blog_idea' | 'blog_outline' | 'blog_draft' | 'email' | 'social'
  platform     text,            -- social only: 'instagram' | 'facebook' | 'linkedin'
  title        text,
  payload      jsonb not null default '{}',
  created_at   timestamptz default now()
);
create index if not exists generated_content_run_idx on generated_content(run_id, content_type);

-- Content calendar items
create table if not exists content_calendar (
  id             uuid primary key default gen_random_uuid(),
  run_id         uuid not null references runs(id) on delete cascade,
  scheduled_date date,
  channel        text not null,   -- 'blog' | 'email' | 'instagram' | 'facebook' | 'linkedin'
  title          text not null,
  description    text,
  keyword        text,
  status         text default 'planned',  -- 'planned' | 'draft' | 'approved' | 'published'
  content_id     uuid references generated_content(id) on delete set null,
  created_at     timestamptz default now()
);
create index if not exists content_calendar_run_idx on content_calendar(run_id);

-- OAuth tokens for Google integrations (GSC, GA4)
create table if not exists integrations (
  id            uuid primary key default gen_random_uuid(),
  domain        text not null,
  provider      text not null,    -- 'gsc' | 'ga4'
  access_token  text,
  refresh_token text,
  token_expiry  timestamptz,
  extra         jsonb default '{}',
  created_at    timestamptz default now(),
  constraint integrations_domain_provider_key unique (domain, provider)
);

-- Cached performance data fetched from GSC / GA4
create table if not exists performance_cache (
  id          uuid primary key default gen_random_uuid(),
  domain      text not null,
  provider    text not null,
  date_range  text not null,
  data        jsonb not null default '{}',
  fetched_at  timestamptz default now(),
  constraint performance_cache_key unique (domain, provider, date_range)
);
