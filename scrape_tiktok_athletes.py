"""
JOOLA TikTok Athlete Scraper via Apify REST API
================================================
Fetches recent TikTok posts from JOOLA-sponsored athletes using
Apify's clockworks/tiktok-scraper actor, then upserts into
the influencer_posts table in Supabase.

USAGE
-----
  python scrape_tiktok_athletes.py               # fetch last 30 posts per athlete
  python scrape_tiktok_athletes.py --posts 50    # fetch more per athlete
  python scrape_tiktok_athletes.py --dry-run     # fetch & print, no DB write

REQUIRED ENV VARS  (already in backend/.env)
---------------------------------------------
  APIFY_TOKEN                Apify API token
  SUPABASE_URL               https://xxxx.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  Supabase service role key
"""

import os
import sys
import json
import time
import argparse
import requests
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
SB_URL      = os.environ.get("SUPABASE_URL", "")
SB_KEY      = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

BRAND_ID    = "04db8591-37a3-4634-9d11-536975fa6935"
APIFY_BASE  = "https://api.apify.com/v2"
ACTOR_ID    = "clockworks~tiktok-scraper"

# Athletes with confirmed TikTok handles
ATHLETES = [
    {"influencer_id": "db4b18f4-da4b-49fd-a1e8-64b52ea8ae3b", "name": "Ben Johns",      "tiktok_handle": "benjohns.pb"},
    {"influencer_id": "9a925fc4-d080-4b40-a566-4de9be16f1b9", "name": "Anna Bright",    "tiktok_handle": "annabright.pb"},
    {"influencer_id": "1eb20d81-3dff-4fcb-ab0f-be93768af354", "name": "Tyson McGuffin", "tiktok_handle": "tysonmcguffinpb"},
]


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


# ── Parsing helpers ───────────────────────────────────────────────────────────
def _parse_ts(raw) -> str | None:
    if not raw:
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(raw, tz=timezone.utc).isoformat()
        s = str(raw).strip()
        try:
            return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").isoformat()
        except ValueError:
            pass
        return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except Exception:
        return None


def _safe_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# ── Apify REST API helpers ────────────────────────────────────────────────────
def _apify_headers() -> dict:
    return {"Authorization": f"Bearer {APIFY_TOKEN}", "Content-Type": "application/json"}


def apify_start_run(run_input: dict) -> str:
    url = f"{APIFY_BASE}/acts/{ACTOR_ID}/runs"
    resp = requests.post(url, headers=_apify_headers(), json=run_input, timeout=30)
    if not resp.ok:
        print(f"  ERROR starting actor: {resp.status_code} — {resp.text[:500]}")
        sys.exit(1)
    run_id = resp.json()["data"]["id"]
    print(f"  Actor run started: {run_id}")
    return run_id


def apify_wait_for_run(run_id: str, timeout_sec: int = 300) -> str:
    url = f"{APIFY_BASE}/actor-runs/{run_id}"
    start = time.time()
    last_status = ""
    while True:
        elapsed = int(time.time() - start)
        if elapsed > timeout_sec:
            print(f"  ERROR: Run timed out after {timeout_sec}s")
            sys.exit(1)
        resp = requests.get(url, headers=_apify_headers(), timeout=15)
        if not resp.ok:
            time.sleep(5)
            continue
        data = resp.json()["data"]
        status = data.get("status", "")
        if status != last_status:
            print(f"  [{elapsed:>3}s] Status: {status}")
            last_status = status
        if status == "SUCCEEDED":
            return data["defaultDatasetId"]
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            print(f"  ERROR: Run {status}.")
            sys.exit(1)
        time.sleep(8)


def apify_fetch_items(dataset_id: str) -> list[dict]:
    url = f"{APIFY_BASE}/datasets/{dataset_id}/items"
    resp = requests.get(url, headers=_apify_headers(), params={"clean": "true", "format": "json"}, timeout=60)
    if not resp.ok:
        print(f"  ERROR fetching dataset: {resp.status_code}")
        sys.exit(1)
    return resp.json()


# ── Scrape one athlete ────────────────────────────────────────────────────────
def scrape_athlete(athlete: dict, max_posts: int) -> list[dict]:
    handle = athlete["tiktok_handle"]
    name   = athlete["name"]
    inf_id = athlete["influencer_id"]
    print(f"\n→ Scraping @{handle} ({name}) …")

    run_input = {
        "profiles": [handle],
        "resultsPerPage": max_posts,
        "shouldDownloadVideos": False,
        "shouldDownloadCovers": False,
    }

    run_id     = apify_start_run(run_input)
    dataset_id = apify_wait_for_run(run_id)
    items      = apify_fetch_items(dataset_id)
    print(f"  Got {len(items)} posts.")

    rows: list[dict] = []
    seen: set[str] = set()

    for item in items:
        post_id = str(item.get("id") or item.get("webVideoUrl", "")[-19:] or "")
        if not post_id or post_id in seen:
            continue
        seen.add(post_id)

        ts = _parse_ts(item.get("createTime") or item.get("createTimeISO"))
        posted = datetime.fromisoformat(ts) if ts else None

        like_count    = _safe_int(item.get("diggCount")    or item.get("likesCount"))
        comment_count = _safe_int(item.get("commentCount") or item.get("comments"))
        view_count    = _safe_int(item.get("playCount")    or item.get("views"))
        share_count   = _safe_int(item.get("shareCount"))

        post_url = item.get("webVideoUrl") or f"https://www.tiktok.com/@{handle}/video/{post_id}"
        caption  = (item.get("text") or item.get("desc") or "")[:2000] or None

        rows.append({
            "influencer_id": inf_id,
            "brand_id":      BRAND_ID,
            "platform":      "tiktok",
            "post_url":      post_url,
            "posted_at":     ts,
            "like_count":    like_count,
            "comment_count": comment_count,
            "view_count":    view_count,
            "caption":       caption,
        })

        date_str = posted.strftime("%b %d, %Y") if posted else "unknown"
        print(f"  {post_id}  likes={like_count}  views={view_count}  {date_str}")

    return rows


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape JOOLA athlete TikToks → Supabase via Apify")
    parser.add_argument("--posts",   type=int, default=30,   help="Max posts per athlete (default 30)")
    parser.add_argument("--dry-run", action="store_true",    help="Fetch only, do not write to Supabase")
    args = parser.parse_args()

    missing = [k for k, v in [
        ("APIFY_TOKEN",               APIFY_TOKEN),
        ("SUPABASE_URL",              SB_URL),
        ("SUPABASE_SERVICE_ROLE_KEY", SB_KEY),
    ] if not v]
    if missing:
        print("ERROR: Missing env vars:", ", ".join(missing))
        sys.exit(1)

    all_rows: list[dict] = []
    for athlete in ATHLETES:
        rows = scrape_athlete(athlete, args.posts)
        all_rows.extend(rows)

    print(f"\n{'='*50}")
    print(f"Total: {len(all_rows)} TikTok posts across {len(ATHLETES)} athletes")

    if args.dry_run:
        print("\n[DRY RUN] First row sample:")
        print(json.dumps(all_rows[0] if all_rows else {}, indent=2, default=str))
        return

    print("\nWriting to Supabase …")
    sb_upsert("influencer_posts", all_rows, on_conflict="post_url")
    print("\nDone! Refresh the Influencers dashboard to see TikTok data.")


if __name__ == "__main__":
    main()
