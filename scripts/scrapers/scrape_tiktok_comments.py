"""JOOLA Pulse — TikTok comment scraper (brand_id-aware).

Fetches comments for JOOLA's TikTok videos and writes them to tiktok_comments
with the correct brand_id sourced from the parent tiktok_videos row.

Strategy:
  1. Load JOOLA videos from tiktok_videos (brand_id = JOOLA_BRAND_ID)
  2. For each video, run Apify 'clockworks/tiktok-comments-scraper'
  3. Upsert results to tiktok_comments with brand_id = video.brand_id

Tables written:
  tiktok_comments  (PK: tiktok_comment_id)

Usage (from backend/ with venv activated):
    python scripts/scrapers/scrape_tiktok_comments.py
    python scripts/scrapers/scrape_tiktok_comments.py --dry-run
    python scripts/scrapers/scrape_tiktok_comments.py --limit 5   # first 5 videos only
    python scripts/scrapers/scrape_tiktok_comments.py --max-comments 50  # per video

Required env vars (backend/.env):
    SUPABASE_URL  or  NEXT_PUBLIC_SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
    APIFY_TOKEN   or  APIFY_API_TOKEN
"""

import argparse
import json as jsonlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ─── Env ──────────────────────────────────────────────────────────────────────

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(BACKEND_ROOT / ".env")

APIFY_TOKEN  = os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

if not all([APIFY_TOKEN, SUPABASE_URL, SUPABASE_KEY]):
    missing = [
        k for k, v in [
            ("APIFY_TOKEN / APIFY_API_TOKEN", APIFY_TOKEN),
            ("SUPABASE_URL / NEXT_PUBLIC_SUPABASE_URL", SUPABASE_URL),
            ("SUPABASE_SERVICE_ROLE_KEY", SUPABASE_KEY),
        ] if not v
    ]
    print(f"ERROR: Missing env vars: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)

# JOOLA brand UUID — sourced from analytics_backend/app/config.py
JOOLA_BRAND_ID = "04db8591-37a3-4634-9d11-536975fa6935"

# Default TikTok handle used when video_url is absent from the DB row
JOOLA_TIKTOK_HANDLE = "joolapickleball"

APIFY_BASE = "https://api.apify.com/v2"
# Apify actor for TikTok comments
TT_COMMENT_ACTOR = "clockworks/tiktok-comments-scraper"

LOG_FILE = BACKEND_ROOT / "logs" / "scrape_tiktok_comments.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

SB_WRITE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
    "User-Agent": "joola-pulse-scraper/1.0",
}
SB_READ_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "User-Agent": "joola-pulse-scraper/1.0",
}

# ─── Logging ──────────────────────────────────────────────────────────────────


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ─── HTTP helpers ─────────────────────────────────────────────────────────────


def http_get(url: str, headers: dict | None = None, timeout: int = 30) -> requests.Response:
    for attempt in range(1, 6):
        try:
            return requests.get(url, headers=headers, timeout=timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            log(f"  ⚠ GET retry {attempt}/5: {e}")
            time.sleep(10)
    raise RuntimeError("GET failed after 5 retries")


def http_post(url: str, headers: dict | None = None, json_data: object = None, timeout: int = 30) -> requests.Response:
    for attempt in range(1, 6):
        try:
            return requests.post(url, headers=headers, json=json_data, timeout=timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            log(f"  ⚠ POST retry {attempt}/5: {e}")
            time.sleep(10)
    raise RuntimeError("POST failed after 5 retries")


# ─── Apify helpers ────────────────────────────────────────────────────────────


def run_actor(actor_id: str, input_data: dict) -> str:
    actor_url = actor_id.replace("/", "~")
    url = f"{APIFY_BASE}/acts/{actor_url}/runs?token={APIFY_TOKEN}"
    for attempt in range(1, 6):
        r = http_post(url, json_data=input_data)
        if r.status_code < 500:
            r.raise_for_status()
            run_id = r.json()["data"]["id"]
            log(f"  ▶ Started {actor_id} → run {run_id}")
            return run_id
        log(f"  ⚠ Apify {r.status_code} retry {attempt}/5 — sleeping 30s")
        time.sleep(30)
    raise RuntimeError(f"run_actor failed after 5 retries: {r.status_code}")


def wait_for_run(run_id: str, poll: int = 20) -> bool:
    url = f"{APIFY_BASE}/actor-runs/{run_id}?token={APIFY_TOKEN}"
    while True:
        d = http_get(url, timeout=15).json()["data"]
        status = d["status"]
        if status == "SUCCEEDED":
            log(f"    ✓ Run {run_id}: SUCCEEDED")
            return True
        if status in ("FAILED", "TIMED-OUT", "ABORTED"):
            log(f"    ✗ Run {run_id}: {status}")
            return False
        log(f"    Run {run_id}: {status}")
        time.sleep(poll)


def fetch_run_items(run_id: str) -> list:
    url = f"{APIFY_BASE}/actor-runs/{run_id}/dataset/items?token={APIFY_TOKEN}&clean=true"
    for attempt in range(1, 6):
        r = http_get(url, timeout=60)
        if r.status_code < 500:
            r.raise_for_status()
            return r.json()
        log(f"  ⚠ Apify {r.status_code} fetching items, retry {attempt}/5 — sleeping 30s")
        time.sleep(30)
    raise RuntimeError(f"fetch_run_items failed after 5 retries: last status {r.status_code}")


# ─── Supabase helpers ─────────────────────────────────────────────────────────


def sb_get_all(table: str, select: str, qs: str = "") -> list:
    out: list = []
    offset = 0
    while True:
        url = (
            f"{SUPABASE_URL}/rest/v1/{table}?select={select}"
            f"{('&' + qs) if qs else ''}&limit=1000&offset={offset}"
        )
        r = http_get(url, headers=SB_READ_HEADERS)
        r.raise_for_status()
        chunk = r.json()
        out.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return out


def sb_upsert(table: str, rows: list, on_conflict: str, dry_run: bool = False) -> int:
    if not rows:
        return 0
    if dry_run:
        log(f"  [DRY-RUN] would upsert {len(rows)} rows → {table}")
        return len(rows)
    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}"
    CHUNK = 200
    written = 0
    for i in range(0, len(rows), CHUNK):
        batch = rows[i: i + CHUNK]
        r = http_post(url, headers=SB_WRITE_HEADERS, json_data=batch, timeout=60)
        if r.status_code in (200, 201, 204):
            written += len(batch)
        else:
            log(f"  ✗ Upsert {table}: {r.status_code} {r.text[:300]}")
    return written


# ─── Step 1: load JOOLA TikTok videos ────────────────────────────────────────


def load_tiktok_videos(limit: int | None) -> list[dict]:
    rows = sb_get_all(
        "tiktok_videos",
        "id,tiktok_video_id,video_url,text,brand_id",
        f"brand_id=eq.{JOOLA_BRAND_ID}&order=posted_at.desc",
    )
    log(f"  Found {len(rows)} JOOLA videos in tiktok_videos")
    if limit:
        rows = rows[:limit]
        log(f"  Limiting to {len(rows)} videos")
    return rows


# ─── Step 2: existing comment IDs (to avoid re-fetching) ─────────────────────


def load_existing_comment_ids(video_id: str) -> set[str]:
    rows = sb_get_all(
        "tiktok_comments",
        "tiktok_comment_id",
        f"video_id=eq.{video_id}",
    )
    return {r["tiktok_comment_id"] for r in rows if r.get("tiktok_comment_id")}


# ─── Step 3: scrape comments for one video ────────────────────────────────────


def scrape_video_comments(video: dict, max_comments: int, dry_run: bool) -> int:
    tiktok_video_id = video.get("tiktok_video_id", "")
    video_url = (
        video.get("video_url")
        or f"https://www.tiktok.com/@{JOOLA_TIKTOK_HANDLE}/video/{tiktok_video_id}"
    )
    video_db_id = video["id"]
    brand_id = video["brand_id"]  # always comes from parent row, never hardcoded

    caption = (video.get("text") or tiktok_video_id or "")[:60]
    log(f"  Video: {caption}")
    log(f"    URL: {video_url}")

    if dry_run:
        log(f"    [DRY-RUN] would fetch up to {max_comments} comments")
        return 0

    run_id = run_actor(TT_COMMENT_ACTOR, {
        "postURLs": [video_url],
        "maxComments": max_comments,
    })

    if not wait_for_run(run_id):
        log(f"    ✗ Apify run failed for {tiktok_video_id}")
        return 0

    items = fetch_run_items(run_id)
    log(f"    Fetched {len(items)} raw comments")

    existing = load_existing_comment_ids(video_db_id)
    now = datetime.now(timezone.utc).isoformat()

    rows: list[dict] = []
    for item in items:
        # Comment ID — try common field names from the TikTok actor response
        comment_id = item.get("id") or item.get("cid") or item.get("commentId")
        if not comment_id or str(comment_id) in existing:
            continue

        # Comment text
        text = item.get("text") or item.get("comment") or ""

        # Author — clockworks actor uses top-level uniqueId; legacy actors use authorMeta.name
        author_meta = item.get("authorMeta") or {}
        author = (
            item.get("uniqueId")              # clockworks/tiktok-comments-scraper
            or author_meta.get("name")
            or item.get("authorText")
            or item.get("author")
            or ""
        )

        # Likes — TikTok actor uses diggCount; fall back to likesCount / likes
        likes = (
            item.get("diggCount")
            or item.get("likesCount")
            or item.get("likes")
            or 0
        )

        # Posted timestamp — prefer ISO string over Unix integer to satisfy TIMESTAMPTZ
        posted = (
            item.get("createTimeISO")          # clockworks actor ISO string
            or item.get("publishedAt")
            or item.get("publishedTime")
            or None
        )

        rows.append({
            "tiktok_comment_id": str(comment_id),
            "video_id":          video_db_id,   # FK to tiktok_videos.id
            "brand_id":          brand_id,       # sourced from parent tiktok_videos row
            "commenter_username": author[:200],
            "comment_text":      text[:2000],
            "comment_likes":     likes,
            "is_brand_reply":    False,
            "posted_at":         posted,
            "scraped_at":        now,
        })

    if rows:
        n = sb_upsert("tiktok_comments", rows, "tiktok_comment_id")
        log(f"    ✓ Upserted {n} new comments (brand_id={brand_id})")
        return n

    log(f"    No new comments to write")
    return 0


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape TikTok comments for JOOLA videos")
    parser.add_argument("--dry-run", action="store_true", help="Preview only — no DB writes or Apify runs")
    parser.add_argument("--limit", type=int, default=None, help="Max videos to process")
    parser.add_argument("--max-comments", type=int, default=200, help="Max comments per video (default: 200)")
    args = parser.parse_args()

    log("=" * 60)
    log(f"JOOLA TikTok Comment Scraper  dry_run={args.dry_run}  max_comments={args.max_comments}")
    log(f"  brand_id: {JOOLA_BRAND_ID}")
    log("=" * 60)

    log("\n[1/2] Loading JOOLA TikTok videos")
    videos = load_tiktok_videos(args.limit)
    if not videos:
        log("  No JOOLA videos found in tiktok_videos — nothing to scrape")
        sys.exit(0)

    log(f"\n[2/2] Scraping comments ({len(videos)} videos × up to {args.max_comments} comments)")
    total = 0
    for idx, video in enumerate(videos, 1):
        log(f"\n  [{idx}/{len(videos)}]")
        n = scrape_video_comments(video, max_comments=args.max_comments, dry_run=args.dry_run)
        total += n
        # Brief pause between videos to avoid Apify rate limits
        if idx < len(videos) and not args.dry_run:
            time.sleep(5)

    log(f"\n{'='*60}")
    log(f"Done. Total comments written: {total}")
    log("=" * 60)


if __name__ == "__main__":
    main()
