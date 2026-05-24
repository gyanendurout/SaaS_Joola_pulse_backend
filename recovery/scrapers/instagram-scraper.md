# Instagram scraper

**Status:** Fully present in repo. The only scraper with end-to-end AI enrichment.

## Location

`frontend/scripts/scrape_joola_ig.py`
Companion files: `scrape_joola_ig.log` (rolling log), `scrape_joola_ig.state.json` (progress checkpoint).

> Naming oddity: the file lives in the **frontend** folder even though it's a Python script. Reason from the file header: it reads env vars from `frontend/.env.local`, so it lives next to that file. Don't move it without updating the path computation.

## What it does

From the file's docstring (`scripts/scrape_joola_ig.py:1-23`):

> Backfills posts & comments since the last scrape, runs OpenAI analysis, and rebuilds derived tables. Idempotent — re-running upserts by primary key.

Tables it writes (all 8 IG tables in the DB):

```
joola_ig_posts             (PK: post_id)
joola_ig_comments          (PK: comment_id)
joola_ig_comment_analysis  (PK: comment_id)
joola_ig_post_analysis     (PK: post_id)
joola_ig_loyal_users       (PK: username)
joola_ig_complaint_log     (PK: comment_id)
joola_ig_wishlist_items    (PK: comment_id)
joola_ig_weekly_snapshot   (PK: week_start)
```

## Dependencies

- Apify actors:
  - `apify/instagram-profile-scraper` — pulls post metadata + stats
  - `apify/instagram-comment-scraper` — pulls comments per post
- OpenAI (`gpt-4o-mini` for per-comment sentiment / topic / complaint / purchase-intent classification)
- Supabase REST API (uses raw `requests` + `Prefer: resolution=merge-duplicates` for upserts)

## Required env vars (read from `frontend/.env.local`)

```
APIFY_API_TOKEN
OPENAI_API_KEY
NEXT_PUBLIC_SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
```

## How to run

```powershell
cd c:\Workspace\SaaS_Joola_pulse\frontend
python scripts/scrape_joola_ig.py
```

Watch the log:
```powershell
Get-Content scripts/scrape_joola_ig.log -Tail 40 -Wait
```

Resume after interruption: the state file (`scrape_joola_ig.state.json`) checkpoints `step`, `posts_scraped`, `comments_scraped`, `comments_analyzed`. Re-running picks up where it left off.

## The target handle

`JOOLA_HANDLE = "joolapickleball"` — hard-coded constant in the script. To scrape a different account, edit the script (it's a single-tenant tool).

## Known issues / gotchas

- **No scheduler.** Must be invoked manually from a dev machine or scheduled by an external runner (Task Scheduler, cron). Project memory mentions adding `cron` to a future Phase-2 task list.
- **BUG-014/015 root cause** lives here: the snapshot pipeline writes `avg_engagement_rate` and `dominant_content_theme` as 0/null. The frontend (`weekly-digest/page.tsx`, `overview/OverviewClient.tsx`) backfills these client-side from `joola_ig_posts` + `joola_ig_post_analysis`. Long-term, fix here, not in the UI.
- The script uses `requests` (sync) — for a large backfill, expect ~30-60 minutes.
- API rate limits: Apify default rate limit is generous, but if you re-run frequently the dataset costs add up. The script tries to fetch only post IDs newer than the latest `posted_at` in `joola_ig_posts` to keep cost down.

## Rebuild from scratch (if file is lost)

If `scrape_joola_ig.py` is deleted, the rebuild outline is:

1. Apify `instagram-profile-scraper` actor input: `{ "usernames": ["joolapickleball"], "resultsType": "posts", "resultsLimit": 200 }`
2. For each post returned, call `instagram-comment-scraper` actor: `{ "directUrls": ["<post_url>"] }`
3. For each comment, OpenAI `chat.completions` call with system prompt:
   > "Classify this Instagram comment about a pickleball brand. Return JSON with: sentiment (positive/negative/neutral/mixed), sentiment_score (-1 to 1), primary_topic (one of: product, athlete, customer-service, complaint, purchase-intent, fan-feedback, generic), emotion, is_complaint (bool), purchase_intent (bool)."
4. Upsert all rows into `joola_ig_*` tables via Supabase REST API with `Prefer: resolution=merge-duplicates` header.
5. Compute weekly rollups: group `joola_ig_posts` by `date_trunc('week', posted_at)`, aggregate counts + avg ER, upsert into `joola_ig_weekly_snapshot`.
