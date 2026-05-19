-- ============================================================
-- Migration 008 — Content Generation (Text v1)
-- Idempotent: safe to run multiple times.
-- Paste into Supabase SQL editor and click Run.
--
-- NOTE: The spec calls this "007_content_generation.sql" but
-- 007_seo_columns.sql already exists. Renumbered to 008.
-- Creates 4 tables (drafts, runs, templates, brand voice),
-- indexes, an updated_at trigger, RLS policies, and seed rows.
-- ============================================================

create extension if not exists "uuid-ossp";

-- ------------------------------------------------------------
-- content_drafts : generated drafts (one row per saved/regenerated draft)
-- ------------------------------------------------------------
create table if not exists content_drafts (
  id                       uuid primary key default uuid_generate_v4(),
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now(),
  created_by               text not null,
  content_type             text not null check (content_type in ('blog','ig_post','twitter_response')),
  status                   text not null default 'draft' check (status in ('draft','approved','published','archived')),
  title                    text,
  body                     text not null,
  hashtags                 text[],
  metadata                 jsonb default '{}'::jsonb,
  source_article_id        uuid references news_articles(id),
  source_signal_snapshot   jsonb not null,
  generation_run_id        uuid,
  parent_draft_id          uuid references content_drafts(id),
  version                  int  not null default 1
);

create index if not exists content_drafts_created_by_status_idx
  on content_drafts(created_by, status);
create index if not exists content_drafts_type_created_idx
  on content_drafts(content_type, created_at desc);
create index if not exists content_drafts_source_article_idx
  on content_drafts(source_article_id);
create index if not exists content_drafts_parent_idx
  on content_drafts(parent_draft_id);

-- updated_at trigger
create or replace function _content_drafts_set_updated_at()
returns trigger as $$
begin
  new.updated_at := now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists content_drafts_updated_at on content_drafts;
create trigger content_drafts_updated_at
  before update on content_drafts
  for each row execute function _content_drafts_set_updated_at();


-- ------------------------------------------------------------
-- content_generation_runs : telemetry per generation invocation
-- ------------------------------------------------------------
create table if not exists content_generation_runs (
  id                  uuid primary key default uuid_generate_v4(),
  created_at          timestamptz not null default now(),
  created_by          text not null,
  content_type        text not null,
  model               text not null,
  prompt_tokens       int,
  completion_tokens   int,
  cost_usd            numeric(10,5),
  latency_ms          int,
  status              text check (status in ('success','error','rate_limited')),
  error_message       text,
  input_signals       jsonb,
  prompt_hash         text
);

create index if not exists content_generation_runs_user_created_idx
  on content_generation_runs(created_by, created_at desc);


-- ------------------------------------------------------------
-- content_templates : reusable prompt scaffolds
-- ------------------------------------------------------------
create table if not exists content_templates (
  id                    uuid primary key default uuid_generate_v4(),
  name                  text not null,
  content_type          text not null,
  system_prompt         text not null,
  user_prompt_template  text not null,
  is_active             boolean default true,
  created_at            timestamptz not null default now()
);


-- ------------------------------------------------------------
-- content_brand_voice : single-row config
-- ------------------------------------------------------------
create table if not exists content_brand_voice (
  id                    uuid primary key default uuid_generate_v4(),
  tone                  text[],
  banned_words          text[],
  signature_phrases     text[],
  default_ctas          text[],
  forbidden_patterns    text[],
  updated_at            timestamptz not null default now()
);


-- ------------------------------------------------------------
-- RLS policies
--   drafts: authenticated read all; writes restricted to created_by
--   runs: service-role only (no anon read)
--   templates / brand_voice: anon read OK, admin-only writes
-- For v1 (no auth yet) we leave RLS enabled but with permissive
-- policies; tighten when auth lands.
-- ------------------------------------------------------------
alter table content_drafts            enable row level security;
alter table content_generation_runs   enable row level security;
alter table content_templates         enable row level security;
alter table content_brand_voice       enable row level security;

-- drafts: read for all (v1), write checks created_by via app layer
drop policy if exists content_drafts_read on content_drafts;
create policy content_drafts_read on content_drafts
  for select using (true);

drop policy if exists content_drafts_write on content_drafts;
create policy content_drafts_write on content_drafts
  for all using (true) with check (true);

-- runs: read-only via service role (no anon)
drop policy if exists content_generation_runs_service on content_generation_runs;
create policy content_generation_runs_service on content_generation_runs
  for all using (true) with check (true);

-- templates + brand_voice: anon read, admin write
drop policy if exists content_templates_read on content_templates;
create policy content_templates_read on content_templates
  for select using (true);
drop policy if exists content_templates_write on content_templates;
create policy content_templates_write on content_templates
  for all using (true) with check (true);

drop policy if exists content_brand_voice_read on content_brand_voice;
create policy content_brand_voice_read on content_brand_voice
  for select using (true);
drop policy if exists content_brand_voice_write on content_brand_voice;
create policy content_brand_voice_write on content_brand_voice
  for all using (true) with check (true);


-- ------------------------------------------------------------
-- Seed: 6 content_templates rows
-- ------------------------------------------------------------
insert into content_templates (name, content_type, system_prompt, user_prompt_template, is_active)
select * from (values
  (
    'Tournament recap',
    'blog',
    'You are the JOOLA Pulse content writer covering pickleball tournaments. Athlete-first, factual, no fabricated scores.',
    'Write a tournament recap blog post.\n\nINPUTS\n- User brief: {user_brief}\n- Primary SEO keyword: {primary_keyword}\n- Tone: {tone}\n- Audience: {audience}\n- Top performing reference posts: {top_posts_context}\n\nFollow the shared OUTPUT structure (H1, meta, intro hook, 3-5 H2 sections, Final word).',
    true
  ),
  (
    'Product launch teaser',
    'ig_post',
    'You are the JOOLA Pulse content writer. Tease product launches without revealing specs not provided in context.',
    'Write a teaser Instagram caption for an upcoming JOOLA product.\n\nINPUTS\n- Brief: {user_brief}\n- Tone: {tone}\n- Audience: {audience}\n- Top reference posts: {top_posts_context}\n\nUse HOOK/BODY/CTA/HASHTAGS/MENTIONS labels.',
    true
  ),
  (
    'Athlete spotlight',
    'ig_post',
    'You are the JOOLA Pulse content writer profiling JOOLA roster athletes. Use only quotes provided in inputs.',
    'Write an athlete spotlight Instagram caption.\n\nINPUTS\n- Brief: {user_brief}\n- Tone: {tone}\n- Audience: {audience}\n- Roster context: provided in conversation\n- Top reference posts: {top_posts_context}\n\nUse HOOK/BODY/CTA/HASHTAGS/MENTIONS labels.',
    true
  ),
  (
    'Crisis response',
    'twitter_response',
    'You are the JOOLA Pulse content writer responding to public criticism. Be factual, calm, never defensive in voice. Always provide an ALTERNATE more conservative version.',
    'Draft a crisis Twitter/X reply.\n\nINPUTS\n- Original tweet/context: {source_tweet_or_article}\n- Reply intent (tone): {tone}\n- Audience: {audience}\n- JOOLA position: {user_brief}\n\nUse REPLY: and ALTERNATE: labels.',
    true
  ),
  (
    'SEO gap fill',
    'blog',
    'You are the JOOLA Pulse content writer filling SEO gaps. Lead with the primary keyword in the first 60 chars of H1.',
    'Write a blog post filling an SEO content gap.\n\nINPUTS\n- User brief: {user_brief}\n- Primary SEO keyword: {primary_keyword}\n- Secondary keywords: {secondary_keywords}\n- Tone: {tone}\n- Audience: {audience}\n- Top reference posts: {top_posts_context}\n\nFollow the shared OUTPUT structure.',
    true
  ),
  (
    'News reaction',
    'ig_post',
    'You are the JOOLA Pulse content writer reacting to industry news. Stay on-brand and never disparage competitors named in the article.',
    'Write an Instagram caption reacting to news.\n\nINPUTS\n- Brief: {user_brief}\n- Tone: {tone}\n- Audience: {audience}\n- News context: {news_article_context}\n- Top reference posts: {top_posts_context}\n\nUse HOOK/BODY/CTA/HASHTAGS/MENTIONS labels.',
    true
  )
) as v(name, content_type, system_prompt, user_prompt_template, is_active)
where not exists (
  select 1 from content_templates t where t.name = v.name and t.content_type = v.content_type
);


-- ------------------------------------------------------------
-- Seed: content_brand_voice (single row)
-- ------------------------------------------------------------
insert into content_brand_voice (tone, banned_words, signature_phrases, default_ctas, forbidden_patterns)
select
  array['informative','hype','celebratory','defensive','educational','promotional'],
  array['crush','destroy','kill','annihilate','smash the competition'],
  array[
    'Athlete-first.',
    'Real players, real wins.',
    'Power, control, precision.'
  ],
  array['Shop the lineup','Sign up for news','Reply with your take','Learn more'],
  array[
    'Unsubstantiated superlatives ("best", "fastest", "world''s") without qualifier',
    'Medical / injury claims ("prevents tennis elbow")',
    'Disparaging competitors by name',
    'Fabricated stats, quotes, tournament results',
    'Aggressive verbs ("crush", "destroy", "kill")',
    'Political content',
    'Athlete quote constructions ("<name> said") unless quote in source context'
  ]
where not exists (select 1 from content_brand_voice);
