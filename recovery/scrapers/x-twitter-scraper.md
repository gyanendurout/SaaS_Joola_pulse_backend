# X / Twitter scraper

**Status:** NOT IN REPO. Likely Apify-based.

## Evidence found

- Tables `x_accounts`, `x_posts` queried at `frontend/app/twitter/page.tsx`.
- CLAUDE.md: "X / Twitter | `/twitter` | `x_accounts` (@joolausa), `x_posts` | 12 posts (all RTs of Tyson McGuffin)".
- CLAUDE.md: "TikTok / X enrichment: 110 TT videos + 12 X posts, all `sentiment_label=null`, `topics=null`, `is_crisis=null`. AI pipeline hasn't run on these tables yet."
- No file matching `twitter*` or `x_*` in `backend/app/` or `frontend/scripts/`.
- Schema is identical to `tiktok_videos` minus `share_count` / `duration_seconds` and plus `retweet_count` / `reply_count`. Same AI enrichment block — currently all null.

## Likely Apify actor

X has been hostile to scraping since Musk's takeover. Apify actors in May 2026 that still work:
- `apidojo/twitter-scraper-lite`
- `gentleman/twitter-search-scraper`
- `quacker/twitter-scraper`

Twitter API v2 is paid ($100/mo Basic tier, with limits) — likely too expensive for 12 posts/week. Apify is more pragmatic.

## Rebuild via Apify

1. **Actor:** `apidojo/twitter-scraper-lite` (or whichever the team confirms is funded).
2. **Input:**
   ```json
   {
     "searchTerms": ["from:joolausa"],
     "maxItems": 200,
     "sort": "Latest"
   }
   ```
3. **Upsert** into `x_posts`:
   ```python
   for item in dataset:
       supabase.table('x_posts').upsert({
           'tweet_id':       item['id'],
           'brand_id':       JOOLA_BRAND_ID,
           'account_id':     <handle to account row>,
           'handle':         item['author']['username'],
           'post_url':       item['url'],
           'text':           item['text'],
           'like_count':     item['likeCount'],
           'retweet_count':  item['retweetCount'],
           'reply_count':    item['replyCount'],
           'view_count':     item['viewCount'],
           'posted_at':      item['createdAt'],
       }).execute()
   ```
4. **Enrichment:** see `ai-enrichment-pipeline.md`.

## Note on RT-only data

CLAUDE.md says all 12 current posts are retweets of Tyson McGuffin. Be aware: most Twitter scrapers conflate RTs with original posts. Add a column or flag if you need to distinguish — currently the schema has no `is_retweet` field. Inferring from `text` starting with `RT @...` is reliable enough.

## Schedule

Recommend weekly Apify Schedule.
