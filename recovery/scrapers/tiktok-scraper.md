# TikTok scraper

**Status:** NOT IN REPO. Per CLAUDE.md, currently runs via Apify.

## Evidence found

- Tables `tiktok_accounts`, `tiktok_videos` are queried at `frontend/app/tiktok/page.tsx`.
- CLAUDE.md states: "TikTok | `/tiktok` | `tiktok_accounts` (@joolapickleball), `tiktok_videos` | 110 videos, 776K views".
- CLAUDE.md: "TikTok / X enrichment: 110 TT videos + 12 X posts, all `sentiment_label=null`, `topics=null`, `is_crisis=null`. **AI pipeline hasn't run on these tables yet.**"
- CLAUDE.md project memory line: "TikTok | Currently via Apify".
- No file matching `tiktok*` exists in `backend/app/` or `frontend/scripts/`.

## Likely Apify actor

Public TikTok scrapers on Apify Store as of May 2026:
- `clockworks/tiktok-scraper` — full-feature, supports profile scraping
- `clockworks/tiktok-profile-scraper` — profile-only, cheaper
- `apify/tiktok-scraper` — official

The repo's Apify token (`apify_api_2QANk...`) is owned by user `7nuiUPNRN29ouzcO6` per env. Log into https://console.apify.com with that user to see which actor was used and on what schedule.

## Inferred behavior from `tiktok_videos` schema

```ts
{
  tiktok_video_id, video_url, text, view_count, like_count,
  comment_count, share_count, duration_seconds, thumbnail_url,
  posted_at, // numeric stats — straight from TikTok JSON
  // AI enrichment block — currently all null:
  sentiment_score, sentiment_label, topics[], brands_mentioned[],
  players_mentioned[], products_mentioned[], is_crisis, is_opportunity,
  purchase_intent_score, crisis_keywords[], enriched_at
}
```

## Rebuild via Apify

1. **Actor:** `clockworks/tiktok-profile-scraper`.
2. **Input:**
   ```json
   {
     "profiles": ["joolapickleball"],
     "resultsPerPage": 200,
     "shouldDownloadVideos": false,
     "shouldDownloadCovers": true
   }
   ```
3. **Run mode:** Schedule via Apify Schedules feature (weekly recommended) OR call via API:
   ```python
   from apify_client import ApifyClient
   client = ApifyClient(os.environ["APIFY_API_TOKEN"])
   run = client.actor("clockworks/tiktok-profile-scraper").call(run_input=input_payload)
   for item in client.dataset(run["defaultDatasetId"]).iterate_items():
       supabase.table('tiktok_videos').upsert({
           'tiktok_video_id': item['id'],
           'brand_id':       JOOLA_BRAND_ID,
           'account_id':     get_account_id_for_handle(item['authorMeta']['name']),
           'handle':         item['authorMeta']['name'],
           'video_url':      item['webVideoUrl'],
           'text':           item['text'],
           'view_count':     item['playCount'],
           'like_count':     item['diggCount'],
           'comment_count':  item['commentCount'],
           'share_count':    item['shareCount'],
           'thumbnail_url':  item['videoMeta']['coverUrl'],
           'posted_at':      datetime.fromtimestamp(item['createTime']),
       }).execute()
   ```
4. **Enrichment:** see `ai-enrichment-pipeline.md` — would need a second pass to fill the AI columns currently null.

## Schedule

CLAUDE.md doesn't state one. Reasonable default: weekly via Apify Schedules.

## Cost note

Apify charges per result for most TikTok actors (typically $0.0005–$0.002 per video). 110 videos costs ~$0.05-$0.20. Negligible.
