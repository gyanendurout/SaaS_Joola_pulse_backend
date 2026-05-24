# YouTube scraper

**Status:** NOT IN REPO. Data exists in the DB but the ingestion code is not committed here.

## Evidence found

- Tables `yt_channels`, `yt_videos`, `yt_channel_weekly`, `yt_comments` are queried at `frontend/app/youtube/page.tsx`.
- CLAUDE.md (May-17 audit): "56 videos, 48K subs" for JOOLA's channel.
- CLAUDE.md (May-19 audit, cross-platform): "**YouTube comments scraper is broken for JOOLA's brand_id** — 2,695 rows total in `yt_comments` but **0 for JOOLA** (990 belong to Engage Pickleball, a competitor)." — confirms a scraper exists somewhere but has a brand-mapping bug.
- No file matching `youtube*` or `yt_*` exists in either `backend/app/` or `frontend/scripts/`.

## Inferred schema implications

`yt_channel_weekly` is rebuilt on a weekly cadence (`week_number`, `year` columns). That implies a weekly schedule runs somewhere — possibly Apify Schedule, possibly an external cron.

## Required components to rebuild

1. **Channel discovery** — given a `brand_id`, look up the `channel_url` on the `yt_channels` row, then extract `channel_id` from it.
2. **Video listing** — YouTube Data API v3 `search.list` with `channelId=<x>` + `order=date` to enumerate uploads. Free quota = 10,000 units/day; `search.list` costs 100 units per page (50 results).
3. **Per-video stats** — `videos.list` part=`statistics,contentDetails,snippet`. 1 unit per video, batched 50/request.
4. **Per-video comments** — `commentThreads.list` part=`snippet`. 1 unit per call. **This is where the broken pipeline lives.** The `brand_id` assignment in the upsert is wrong — the existing code is mapping comments to the wrong brand.
5. **Weekly snapshot** — every Sunday at 06:00 UTC, query channel `statistics` and write `subscribers`, `total_views`, `total_videos` into `yt_channel_weekly` with `week_number`, `year` from today's date.

## Rebuild outline

Use the **YouTube Data API v3** with an API key (not OAuth — read-only public data). Service: https://console.cloud.google.com → APIs & Services → YouTube Data API v3 → enable + create API key.

```python
# pseudocode
def scrape_yt_for_brand(brand_id):
    channel = supabase.table('yt_channels').select('*').eq('brand_id', brand_id).single().execute().data
    channel_id = channel['channel_id']
    # 1. list uploads
    uploads = youtube.search().list(channelId=channel_id, order='date', maxResults=50, type='video').execute()
    # 2. fetch stats for each video
    for vid in uploads.items:
        stats = youtube.videos().list(part='statistics,contentDetails,snippet', id=vid.id.videoId).execute()
        supabase.table('yt_videos').upsert({
            'youtube_video_id': vid.id.videoId,
            'channel_id': channel['id'],         # internal UUID
            'brand_id':   channel['brand_id'],   # ← THIS is the column the broken script gets wrong
            'title':      stats.items[0].snippet.title,
            ...
        }).execute()
    # 3. weekly snapshot
    if datetime.utcnow().weekday() == 6:  # Sunday
        snap = youtube.channels().list(part='statistics', id=channel_id).execute()
        supabase.table('yt_channel_weekly').upsert({...}).execute()
```

## Fixing the comment-brand-id bug

When you rewrite the comment scraper, ensure every `yt_comments` row gets `brand_id` from the parent video's `brand_id`, not from a fallback / hardcoded value. Smoke test: after a run, `select count(*) from yt_comments where brand_id = '04db8591-37a3-4634-9d11-536975fa6935';` should be > 0.

## YouTube API quota

10,000 units/day is enough for a daily incremental scrape of one channel + 200 videos. For initial backfill of 56 videos + comments, expect ~3,000-5,000 units. Plan accordingly.
