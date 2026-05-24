# Reddit scraper

**Status:** NOT IN REPO. Data exists in the DB but the ingestion code is not committed here.

## Evidence found

- Table `reddit_mentions` is queried at `frontend/app/reddit/page.tsx`.
- CLAUDE.md audit (May-19 12:40p) reports: "Reddit enrichment is partial: 119/123 have `topics` populated, 17 are `is_crisis=true`, 58 are `is_opportunity=true`, but **all 123 have `sentiment=null`** — only the topic/crisis/opp pipeline has run."
- No file matching `reddit*` exists under `backend/app/` or `frontend/scripts/`.
- Apify operator user ID `7nuiUPNRN29ouzcO6` is documented in env — there are publicly-listed Apify Reddit actors (`trudax/reddit-scraper-lite`, `apify/reddit-scraper`) that could be the source.

## Inferred behavior from `reddit_mentions` columns

| Column | Implies |
|---|---|
| `reddit_post_id`, `subreddit`, `post_url`, `post_title`, `content_text`, `author`, `upvotes`, `posted_at` | Raw scrape data — straight from Reddit JSON API or an Apify actor |
| `country_code` | Subreddit-level country mapping (regional subs) |
| `content_type` | "submission" vs "comment" most likely |
| `competitor_switch boolean`, `switch_direction` | Custom enrichment — detects users moving FROM JOOLA TO competitor (or vice versa) |
| `sentiment` | Never populated (gap) |
| `topics text[]`, `brands_mentioned text[]`, `players_mentioned text[]`, `is_crisis boolean`, `is_opportunity boolean` | AI enrichment — populated for 119/123 rows |

## Rebuild sketch

### Option A — Apify

1. Use actor `trudax/reddit-scraper-lite` or `apify/reddit-scraper`.
2. Input: search for `JOOLA` plus each JOOLA athlete name + each JOOLA product keyword across the relevant subreddits (`r/Pickleball`, `r/PickleballPaddles`, `r/PicklePros`).
3. Filter results posted within the last 180 days.
4. For each result, upsert into `reddit_mentions` with `brand_id = '04db8591-37a3-4634-9d11-536975fa6935'`.

### Option B — Reddit API directly

`praw` library. Subscribe to subreddit feeds, look for keyword matches, store new posts/comments. Reddit API rate limit ~100 requests per minute per OAuth client — sufficient for a daily pull.

### Search queries to seed

JOOLA brand mentions + the same 35-player list defined in `news_scraper.py:38-48` + the 11 product keywords (perseus, scorpeus, hyperion, etc., see `seed-data.md`).

### Enrichment to run after raw ingestion

For each new `reddit_mentions` row, call OpenAI with a prompt like:

> "Classify this Reddit post/comment about pickleball brand JOOLA. Return JSON with: sentiment ('positive'|'negative'|'neutral'|'mixed'), topics (text[]), brands_mentioned (text[]), players_mentioned (text[]), is_crisis (bool — true if this post could escalate to PR risk), is_opportunity (bool — true if this represents purchase intent or recruitment opportunity), competitor_switch (bool), switch_direction ('to_joola'|'from_joola'|null)."

Use `gpt-4o-mini` for cost. Cost-cap at $0.005 per row.

## Schedule

CLAUDE.md does not mention a schedule. Recommended: weekly cron via the backend's existing FastAPI surface — add `POST /api/reddit/scrape` analogous to `/api/news/scrape`.
