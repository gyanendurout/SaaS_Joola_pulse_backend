-- ============================================================
-- SEO Intel — migration 007
-- Add display columns for SEO dashboard Phase 1
-- Run in Supabase SQL editor (idempotent)
-- ============================================================

-- 1. Add run_date to runs (dashboard date display)
alter table runs add column if not exists run_date timestamptz;
update runs set run_date = coalesce(finished_at, created_at) where run_date is null;

-- 2. Add seed_keywords list to runs (curated keyword management)
alter table runs add column if not exists seed_keywords text[] default '{}';

-- 3. Add display columns to issues (populated by agents; used by dashboard)
alter table issues add column if not exists issue_type   text;
alter table issues add column if not exists title        text;
alter table issues add column if not exists description  text;
alter table issues add column if not exists category     text;
alter table issues add column if not exists url          text;

-- Backfill title from issue_code for existing rows
update issues
set
  title = initcap(replace(replace(coalesce(issue_code, ''), '_', ' '), '-', ' ')),
  category = case
    when coalesce(issue_code,'') ~* 'TITLE|META|H1|HEADING|CANONICAL|ROBOTS|OG_' then 'On-Page'
    when coalesce(issue_code,'') ~* 'SPEED|LCP|CLS|FCP|PERF|CORE_WEB' then 'Performance'
    when coalesce(issue_code,'') ~* 'BROKEN|REDIRECT|404|STATUS|CRAWL|NOINDEX|BLOCKED' then 'Technical'
    when coalesce(issue_code,'') ~* 'CONTENT|THIN|WORD|DUPLICATE|DUPE' then 'Content'
    when coalesce(issue_code,'') ~* 'SCHEMA|STRUCTURED|JSON_LD' then 'Structured Data'
    else 'Technical'
  end
where title is null;

-- 4. Add keyword quality columns to domain_ranked_keywords
alter table domain_ranked_keywords add column if not exists difficulty        int;
alter table domain_ranked_keywords add column if not exists previous_position int;
alter table domain_ranked_keywords add column if not exists is_gap            boolean not null default false;
alter table domain_ranked_keywords add column if not exists intent            text;

-- 5. Create SEO health score view (recalculates live from issues)
create or replace view seo_health_scores as
select
  r.id                                                                                as run_id,
  r.canonical_domain,
  coalesce(r.run_date, r.finished_at, r.created_at)                                  as run_date,
  r.status,
  count(i.id)                                                                         as total_issues,
  count(i.id) filter (where i.severity = 'critical')                                 as critical_count,
  count(i.id) filter (where i.severity = 'high')                                     as high_count,
  count(i.id) filter (where i.severity = 'medium')                                   as medium_count,
  count(i.id) filter (where i.severity in ('low','info'))                            as low_count,
  count(dk.id)                                                                        as total_keywords,
  count(dk.id) filter (where dk.position between 1 and 3)                           as top3_count,
  count(dk.id) filter (where dk.position between 1 and 10)                          as top10_count,
  greatest(0,
    100
    - count(i.id) filter (where i.severity = 'critical') * 5
    - count(i.id) filter (where i.severity = 'high') * 2
    - count(i.id) filter (where i.severity = 'medium') * 1
  )::int                                                                              as health_score
from runs r
left join issues i on i.run_id = r.id
left join domain_ranked_keywords dk on dk.run_id = r.id
group by r.id, r.canonical_domain, r.run_date, r.finished_at, r.created_at, r.status;
