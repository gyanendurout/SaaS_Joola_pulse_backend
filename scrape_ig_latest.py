"""
JOOLA Instagram Scraper via Apify
===================================
Fetches the most recent posts and comments from @joolapickleball using
Apify's instagram-scraper actor, then upserts into Supabase.

USAGE
-----
  python scrape_ig_latest.py                  # fetch last 50 posts
  python scrape_ig_latest.py --posts 100      # fetch more posts
  python scrape_ig_latest.py --dry-run        # fetch & print, no DB write

REQUIRED ENV VARS  (already in backend/.env)
---------------------------------------------
  APIFY_TOKEN                Apify API token
  SUPABASE_URL               https://xxxx.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  Supabase service role key
"""

import os
import sys
import json
import argparse
import requests
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from apify_client import ApifyClient

APIFY_TOKEN    = os.environ.get("APIFY_TOKEN", "")
SB_URL         = os.environ.get("SUPABASE_URL", "")
SB_KEY         = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

IG_PROFILE_URL = "https://www.instagram.com/joolapickleball/"


# ── Supabase helpers ──────────────────────────────────────────────────────────
def _sb_headers() -> dict:
    return {
        "apikey": SB_KEY,
        "Authorization": f"Bearer {SB_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }


def sb_upsert(table: str, rows: list[dict], on_conflict: str, dry_run: bool = False) -> None:
    if not rows:
        return
    if dry_run:
        print(f"  [DRY RUN] Would upsert {len(rows)} rows into {table}")
        return
    for i in range(0, len(rows), 200):
        chunk = rows[i : i + 200]
        resp = requests.post(
            f"{SB_URL}/rest/v1/{table}?on_conflict={on_conflict}",
            headers=_sb_headers(),
            json=chunk,
            timeout=30,
        )
        if not resp.ok:
            print(f"  [WARN] {table} chunk {i//200+1}: {resp.status_code} — {resp.text[:300]}")
        else:
            print(f"  +  {table}: upserted {len(chunk)} rows")


# ── Data helpers ──────────────────────────────────────────────────────────────
def _post_type(media_type: str) -> str:
    t = (media_type or "").lower()
    if t == "video":   return "reel"
    if t == "sidecar": return "carousel"
    return "image"


def _parse_ts(raw) -> str | None:
    if not raw:
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.utcfromtimestamp(raw).isoformat() + "+00:00"
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).isoformat()
    except Exception:
        return None


# ── Apify run ─────────────────────────────────────────────────────────────────
def run_apify_scraper(max_posts: int) -> list[dict]:
    print(f"\nStarting Apify instagram-scraper for {IG_PROFILE_URL} (limit {max_posts}) …")
    client = ApifyClient(APIFY_TOKEN)
    run = client.actor("apify/instagram-scraper").call(
        run_input={
            "directUrls":    [IG_PROFILE_URL],
            "resultsType":   "posts",
            "resultsLimit":  max_posts,
            "addParentData": False,
        },
        timeout_secs=300,
    )
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    print(f"  Actor finished. Got {len(items)} items.")
    return items


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape @joolapickleball → Supabase via Apify")
    parser.add_argument("--posts",   type=int, default=50,  help="Max posts to fetch (default 50)")
    parser.add_argument("--dry-run", action="store_true",   help="Fetch only, do not write to Supabase")
    args = parser.parse_args()

    missing = [k for k, v in [
        ("APIFY_TOKEN",               APIFY_TOKEN),
        ("SUPABASE_URL",              SB_URL),
        ("SUPABASE_SERVICE_ROLE_KEY", SB_KEY),
    ] if not v]
    if missing:
        print("ERROR: Missing env vars:", ", ".join(missing))
        sys.exit(1)

    items = run_apify_scraper(args.posts)
    if not items:
        print("No items returned from Apify. Check actor run logs at console.apify.com.")
        sys.exit(1)

    post_rows: list[dict]    = []
    comment_rows: list[dict] = []

    for idx, item in enumerate(items, 1):
        media_id      = str(item.get("id") or item.get("shortCode", ""))
        ts            = _parse_ts(item.get("timestamp"))
        posted        = datetime.fromisoformat(ts) if ts else None

        like_count    = item.get("likesCount") or 0
        comment_count = item.get("commentsCount") or 0
        view_count    = item.get("videoViewCount") or item.get("videoPlayCount") or 0
        # ER proxy: (likes + comments) / views for video; 0 otherwise (no reach from Apify)
        er            = round((like_count + comment_count) / view_count, 6) if view_count > 0 else 0.0

        post_rows.append({
            "post_id":         media_id,
            "post_type":       _post_type(item.get("type", "")),
            "caption":         (item.get("caption") or "")[:2000] or None,
            "thumbnail_url":   item.get("displayUrl"),
            "post_url":        item.get("url"),
            "like_count":      like_count,
            "comment_count":   comment_count,
            "view_count":      view_count,
            "engagement_rate": er,
            "posted_at":       ts,
            "day_of_week":     posted.strftime("%A") if posted else None,
            "hour_of_day":     posted.hour            if posted else None,
        })

        for c in item.get("latestComments") or []:
            comment_rows.append({
                "comment_id":   str(c.get("id", "")),
                "post_id":      media_id,
                "username":     c.get("ownerUsername", ""),
                "comment_text": (c.get("text") or "")[:2000],
                "commented_at": _parse_ts(c.get("timestamp")),
            })

        date_str = posted.strftime("%b %d, %Y") if posted else "unknown date"
        print(f"  [{idx:>3}/{len(items)}] {media_id}  likes={like_count}  comments={comment_count}  {date_str}")

    print(f"\nSummary: {len(post_rows)} posts · {len(comment_rows)} comments")

    if args.dry_run:
        print("\n[DRY RUN] First post row:")
        print(json.dumps(post_rows[0] if post_rows else {}, indent=2, default=str))
        return

    print("\nWriting to Supabase …")
    sb_upsert("joola_ig_posts",    post_rows,    on_conflict="post_id")
    sb_upsert("joola_ig_comments", comment_rows, on_conflict="comment_id")
    print("\nDone! Refresh the dashboard to see updated data.")
    print("Tip: run the AI analysis pipeline next to regenerate comment_analysis and post_analysis.")


if __name__ == "__main__":
    main()
