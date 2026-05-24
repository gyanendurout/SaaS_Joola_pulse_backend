# JOOLA Pulse — Supabase Schema Overview

The DB has two distinct schema lineages:

1. **Migration-backed** — files in `migrations/` create these tables on a fresh project.
2. **Inferred-only** — used by the frontend / IG scraper / social-media pages but **have no migration file** in the repo. Schemas below are reconstructed from TypeScript types and Python scraper code. You will need to write the `CREATE TABLE` statements before scrapers run successfully.

Project memory (`CLAUDE.md`) calls out **78 tables**. The repo contains migrations for ~21 of them.

---

## Migration-backed tables (SEO + News)

### SEO core (migration 001)
| Table | Purpose | Key columns |
|---|---|---|
| `runs` | One row per SEO project run | `id`, `website_url`, `canonical_domain`, `market`, `crawl_mode`, `status`, `pages_crawled`, `started_at`, `finished_at` |
| `pages` | One row per crawled URL | `run_id`, `url`, `http_status`, `title`, `meta_description`, `h1[]`, `canonical`, `word_count`, `text_content`, `page_type`, `internal_links[]` |
| `issues` | SEO issues per page (or site-wide if page_id null) | `run_id`, `page_id`, `issue_code`, `severity`, `details jsonb`, `recommendation` |
| `entities` | Products / categories / services / brands / topics extracted by AI | `run_id`, `entity_type`, `canonical_name`, `confidence`, `attributes` |
| `keywords` | Keyword research results (DataForSEO) | `run_id`, `keyword`, `search_volume`, `cpc`, `intent`, `seed_entity_id`, `suggested_action` |
| `jobs` | Background job tracking | `run_id`, `job_type`, `status`, `progress`, `result` |

### SEO extended (migration 003)
| Table | Purpose |
|---|---|
| `serp_results` | SERP organic rows per keyword per run |
| `backlinks_summary` | Domain-level backlink metrics |
| `competitor_domains` | Organic competitors from DataForSEO |
| `domain_ranked_keywords` | Actual SERP positions the domain holds (with `difficulty`, `previous_position`, `is_gap` from migration 007) |
| `gap_analyses` | Delta between this run and the previous one |

### SEO content (migration 005)
| Table | Purpose |
|---|---|
| `generated_content` | Blog ideas, outlines, drafts, emails, social posts |
| `content_calendar` | Scheduled content items |
| `integrations` | OAuth tokens for GSC / GA4 |
| `performance_cache` | Cached GSC / GA4 data |

### SEO display columns (migration 007)
- Adds `run_date`, `seed_keywords[]` to `runs`
- Adds `issue_type`, `title`, `description`, `category`, `url` to `issues`
- Adds `difficulty`, `previous_position`, `is_gap`, `intent` to `domain_ranked_keywords`
- Creates `seo_health_scores` VIEW (live health-score computation)

### News Intelligence (migration 006)
| Table | Purpose | Notes |
|---|---|---|
| `news_articles` | One row per scraped article (unique on `url`) | sentiment, importance, entity detection (JOOLA/players/competitors), AI summary |
| `news_scrape_runs` | One row per scrape job | counts of sites scraped, articles found/new/with-mentions |
| `news_sources` | Site registry (20 sites pre-seeded by the migration) | authority_score, last_success_at, last_failed_at |
| `news_scrape_errors` | Per-site error log linked to scrape run | |

---

## Tables with no migration source (CRITICAL — recreate manually)

The following tables are queried in code but have no `CREATE TABLE` in `migrations/`. After fresh DB, you must write `008_social_and_ig_tables.sql` containing the statements below.

### Multi-tenancy root

#### `brands` — JOOLA + 10 competitor brands
Inferred from `brand_id = '04db8591-37a3-4634-9d11-536975fa6935'` used as a constant in every social-media `page.tsx`, and CLAUDE.md mention of "11 brands incl. JOOLA".

Suggested columns:
```sql
create table brands (
  id          uuid primary key default gen_random_uuid(),
  name        text not null unique,
  slug        text,
  is_joola    boolean not null default false,
  website_url text,
  created_at  timestamptz not null default now()
);
```
Competitor brands enumerated in `backend/app/agents/news_scraper.py:50-53`:
`Selkirk, Paddletek, Franklin, CRBN, Engage, Onix, Six Zero, Proton, Head, Wilson`.

### Instagram (8 tables, prefix `joola_ig_`)

From `frontend/scripts/scrape_joola_ig.py` docstring and frontend page queries:

#### `joola_ig_posts` — PK `post_id`
Columns referenced: `post_id`, `post_type`, `engagement_rate` (0-1 fraction), `like_count`, `comment_count`, `view_count`, `posted_at`, `day_of_week`, `hour_of_day`, `thumbnail_url`, `caption`, `athletes_shown text[]`.

#### `joola_ig_comments` — PK `comment_id`
`comment_id`, `post_id` (FK), `username`, `comment_text`, `commented_at`.

#### `joola_ig_comment_analysis` — PK `comment_id`
AI enrichment of each comment: `sentiment`, `sentiment_score`, `primary_topic`, `emotion`, `is_complaint`, `purchase_intent`, plus several other classifier outputs.

#### `joola_ig_post_analysis` — PK `post_id`
AI analysis of post content: `post_id`, `content_theme`, `post_intent`, `sentiment_tone`.

#### `joola_ig_loyal_users` — PK `username`
Fan profile: `username`, `loyalty_tier` (`SUPER_FAN` / others), `ambassador_score`, `is_potential_ambassador`, `active_months`, `avg_sentiment_score`.

#### `joola_ig_complaint_log` — PK `comment_id`
`comment_id`, `username`, `complaint_text`, `complaint_category`, `severity`, `joola_responded boolean`, `complained_at`.

#### `joola_ig_wishlist_items` — PK `comment_id`
Feature requests: `comment_id`, `wishlist_text`, `category`, `username`.

#### `joola_ig_weekly_snapshot` — PK `week_start`
Weekly rollup: `week_start`, `posts_published`, `total_comments`, `total_views`, `avg_engagement_rate`, `complaint_count`, `purchase_intent_count`, `dominant_content_theme` (often null today — BUG-015 backfills in-app).

### YouTube (3 tables)

Schema fully captured in `frontend/app/youtube/page.tsx` TypeScript types:

#### `yt_channels` — one row per channel per brand
`id`, `brand_id`, `channel_id`, `channel_name`, `channel_url`, `region`, `country_code`, `is_primary boolean`, `is_active boolean`.

#### `yt_videos` — per-video metadata + stats
`id`, `youtube_video_id`, `channel_id`, `brand_id`, `title`, `video_url`, `thumbnail_url`, `published_at`, `duration_seconds`, `video_type`, `view_count`, `like_count`, `comment_count`, `is_short boolean`, `is_sponsored boolean`.

#### `yt_channel_weekly` — weekly rollups
`id`, `channel_id`, `brand_id`, `subscribers`, `total_views`, `total_videos`, `videos_uploaded_this_week`, `avg_views_last_10_videos`, `week_number int`, `year int`, `scraped_at`.

#### `yt_comments` — (mentioned in CLAUDE.md audit)
Currently broken — 2,695 total rows but 0 for JOOLA brand_id. Schema not visible in frontend; infer from analogous platform tables: `id`, `youtube_video_id`, `brand_id`, `commenter`, `text`, `like_count`, `posted_at`, plus AI enrichment columns matching the IG pattern.

### TikTok (2 tables)

Schema in `frontend/app/tiktok/page.tsx`:

#### `tiktok_accounts`
`id`, `brand_id`, `handle`, `profile_url`, `created_at`.

#### `tiktok_videos`
`id`, `account_id`, `brand_id`, `handle`, `tiktok_video_id`, `video_url`, `text`, `view_count`, `like_count`, `comment_count`, `share_count`, `duration_seconds`, `thumbnail_url`, `posted_at`, `created_at`. Plus AI enrichment (all null today): `sentiment_score`, `sentiment_label`, `topics text[]`, `brands_mentioned text[]`, `players_mentioned text[]`, `products_mentioned text[]`, `is_crisis boolean`, `is_opportunity boolean`, `purchase_intent_score`, `crisis_keywords text[]`, `enriched_at`.

### X / Twitter (2 tables)

Schema in `frontend/app/twitter/page.tsx`:

#### `x_accounts`
`id`, `brand_id`, `handle`, `profile_url`, `created_at`.

#### `x_posts`
`id`, `account_id`, `brand_id`, `handle`, `tweet_id`, `post_url`, `text`, `like_count`, `retweet_count`, `reply_count`, `view_count`, `posted_at`, `created_at`. Same AI-enrichment block as TikTok above. All AI columns null today.

### Reddit (1 table)

Schema in `frontend/app/reddit/page.tsx`:

#### `reddit_mentions`
`id`, `brand_id`, `reddit_post_id`, `subreddit`, `country_code`, `post_title`, `post_url`, `content_type`, `content_text`, `author`, `upvotes int`, `posted_at`, `sentiment` (all-null today), `competitor_switch boolean`, `switch_direction`, `scraped_at`, `topics text[]`, `brands_mentioned text[]`, `players_mentioned text[]`, `is_crisis boolean`, `is_opportunity boolean`.

CLAUDE.md audit: 119/123 have `topics`, 17 flagged `is_crisis`, 58 `is_opportunity`, all 123 have `sentiment = null` (only topic/crisis/opp pipeline ran).

### Influencers (2 tables)

Schema in `frontend/app/influencers/page.tsx`:

#### `influencers`
`id`, `brand_id`, `name`, `type` (e.g. "athlete"), `instagram_handle`, `youtube_channel_url`, `tiktok_handle`, `follower_count_ig`, `follower_count_yt`, `country_code`, `contract_type`, `is_active boolean`.

#### `influencer_posts`
`id`, `influencer_id`, `brand_id`, `platform`, `post_url`, `posted_at`, `like_count`, `comment_count`, `view_count`, `caption`, `hashtags text[]`, `is_sponsored boolean`, `sentiment`, `scraped_at`.

CLAUDE.md verified: 54 JOOLA `influencer_posts` (not the earlier-reported 146).

---

## Foreign-key topology summary

```
brands  ─┬─►  influencers ─►  influencer_posts
         ├─►  joola_ig_*        (FK = post_id or username — NOT a brand_id since these tables are all-JOOLA)
         ├─►  yt_channels   ─►  yt_videos, yt_channel_weekly, yt_comments
         ├─►  tiktok_accounts ─► tiktok_videos
         ├─►  x_accounts     ─►  x_posts
         └─►  reddit_mentions

runs ─┬─►  pages ─► (issues, ...)
      ├─►  issues
      ├─►  entities ─► keywords
      ├─►  serp_results, backlinks_summary, competitor_domains
      ├─►  domain_ranked_keywords
      ├─►  gap_analyses
      ├─►  generated_content ─► content_calendar
      └─►  jobs

news_scrape_runs ─►  news_scrape_errors
news_sources        (standalone)
news_articles       (standalone, unique on url)
```

---

## "78 tables" reconciliation

CLAUDE.md says 78 tables. The codebase backs:
- 8 SEO core (migration 001)
- 5 SEO extended (003)
- 4 SEO content + integrations (005)
- 4 News Intelligence (006)
- 1 SEO health VIEW (007)
- **= 22 with migrations**

Inferred from frontend/scraper code (no migration):
- 1 `brands`
- 8 `joola_ig_*`
- 4 YouTube
- 2 TikTok
- 2 X/Twitter
- 1 Reddit
- 2 Influencers
- **= 20 without migrations**

**Subtotal: 42 tables.** The remaining ~36 referenced in CLAUDE.md's "78 tables" claim do not appear in either migrations or queried frontend code in this repo. Likely candidates:
- Per-platform analysis tables analogous to `joola_ig_comment_analysis` for each social platform
- Hashtag / mention / topic dimension tables
- Authentication / user-management tables (Supabase Auth provides some automatically — `auth.users`, etc.)
- Historical / snapshot tables not yet wired to the UI

Audit the production DB via Supabase Studio → Database → Tables to enumerate the actual set, then update this document.
