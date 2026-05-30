"""JOOLA Pulse — X (Twitter) reply scraper (brand_id-aware).

Fetches replies for JOOLA's X/Twitter posts and writes them to x_replies
with the correct brand_id sourced from the parent x_posts row.

Strategy:
  1. Load JOOLA posts from x_posts (brand_id = JOOLA_BRAND_ID)
  2. For each post, run Apify 'quacker/twitter-scraper' searching by conversation_id
  3. Upsert results to x_replies with brand_id = post.brand_id

Tables written:
  x_replies  (PK: tweet_reply_id)

Usage (from backend/ with venv activated):
    python scripts/scrapers/scrape_x_replies.py
    python scripts/scrapers/scrape_x_replies.py --dry-run
    python scripts/scrapers/scrape_x_replies.py --limit 5        # first 5 posts only
    python scripts/scrapers/scrape_x_replies.py --max-replies 200  # per post (default: 200)

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

APIFY_BASE = "https://api.apify.com/v2"
# Apify actor for Twitter/X scraping
X_REPLY_ACTOR = "quacker/twitter-scraper"

LOG_FILE = BACKEND_ROOT / "logs" / "scrape_x_replies.log"
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
    r = http_get(url, timeout=60)
    r.raise_for_status()
    return r.json()


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


# ─── Step 1: load JOOLA X posts ───────────────────────────────────────────────


def load_x_posts(limit: int | None) -> list[dict]:
    rows = sb_get_all(
        "x_posts",
        "id,tweet_id,post_url,text,brand_id",
        f"brand_id=eq.{JOOLA_BRAND_ID}&order=posted_at.desc",
    )
    log(f"  Found {len(rows)} JOOLA posts in x_posts")
    if limit:
        rows = rows[:limit]
        log(f"  Limiting to {len(rows)} posts")
    return rows


# ─── Step 2: existing reply IDs (to avoid re-fetching) ───────────────────────


def load_existing_reply_ids(post_id: str) -> set[str]:
    rows = sb_get_all(
        "x_replies",
        "tweet_reply_id",
        f"post_id=eq.{post_id}",
    )
    return {r["tweet_reply_id"] for r in rows if r.get("tweet_reply_id")}


# ─── Step 3: scrape replies for one post ──────────────────────────────────────


def scrape_post_replies(post: dict, max_replies: int, dry_run: bool) -> int:
    tweet_id = post.get("tweet_id")
    post_db_id = post["id"]
    brand_id = post["brand_id"]  # always comes from parent row, never hardcoded
    snippet = (post.get("text") or "")[:60]

    if not tweet_id:
        log(f"  ⚠ Post {post_db_id} has no tweet_id — skipping")
        return 0

    log(f"  Post: {snippet!r}")
    log(f"    tweet_id: {tweet_id}")

    if dry_run:
        log(f"    [DRY-RUN] would fetch up to {max_replies} replies for conversation_id:{tweet_id}")
        return 0

    run_id = run_actor(X_REPLY_ACTOR, {
        "searchTerms": [f"conversation_id:{tweet_id}"],
        "maxTweets": max_replies,
        "includeReplies": True,
    })

    if not wait_for_run(run_id):
        log(f"    ✗ Apify run failed for tweet_id={tweet_id}")
        return 0

    items = fetch_run_items(run_id)
    log(f"    Fetched {len(items)} raw items")

    existing = load_existing_reply_ids(post_db_id)
    now = datetime.now(timezone.utc).isoformat()

    rows: list[dict] = []
    for item in items:
        reply_id = (
            item.get("id")
            or item.get("tweetId")
            or item.get("id_str")
        )
        if not reply_id:
            continue

        reply_id_str = str(reply_id)

        # Skip the original tweet itself — we only want replies
        if reply_id_str == str(tweet_id):
            continue

        if reply_id_str in existing:
            continue

        text = item.get("text") or item.get("full_text") or ""

        # Extract author from nested or flat fields
        author_obj = item.get("author") or {}
        author = (
            author_obj.get("username")
            or item.get("user", {}).get("screen_name")
            or item.get("username")
            or ""
        )

        likes = (
            item.get("likeCount")
            or item.get("favorite_count")
            or item.get("favoriteCount")
            or 0
        )
        retweets = (
            item.get("retweetCount")
            or item.get("retweet_count")
            or 0
        )
        posted_at = item.get("createdAt") or item.get("created_at") or None

        rows.append({
            "tweet_reply_id":  reply_id_str,
            "post_id":         post_db_id,    # FK to x_posts.id (UUID)
            "brand_id":        brand_id,      # sourced from parent x_posts row
            "replier_username": author[:200],
            "reply_text":      text[:2000],
            "reply_likes":     likes,
            "retweet_count":   retweets,
            "is_brand_reply":  False,
            "posted_at":       posted_at,
            "scraped_at":      now,
        })

    if rows:
        n = sb_upsert("x_replies", rows, "tweet_reply_id")
        log(f"    ✓ Upserted {n} new replies (brand_id={brand_id})")
        return n

    log(f"    No new replies to write")
    return 0


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape X/Twitter replies for JOOLA posts")
    parser.add_argument("--dry-run", action="store_true", help="Preview only — no DB writes or Apify runs")
    parser.add_argument("--limit", type=int, default=None, help="Max posts to process")
    parser.add_argument("--max-replies", type=int, default=200, help="Max replies per post (default: 200)")
    args = parser.parse_args()

    log("=" * 60)
    log(f"JOOLA X Reply Scraper  dry_run={args.dry_run}  max_replies={args.max_replies}")
    log(f"  brand_id: {JOOLA_BRAND_ID}")
    log("=" * 60)

    log("\n[1/2] Loading JOOLA X posts")
    posts = load_x_posts(args.limit)
    if not posts:
        log("  No JOOLA posts found in x_posts — nothing to scrape")
        sys.exit(0)

    log(f"\n[2/2] Scraping replies ({len(posts)} posts × up to {args.max_replies} replies)")
    total = 0
    for idx, post in enumerate(posts, 1):
        log(f"\n  [{idx}/{len(posts)}]")
        n = scrape_post_replies(post, max_replies=args.max_replies, dry_run=args.dry_run)
        total += n
        # Brief pause between posts to avoid Apify rate limits
        if idx < len(posts) and not args.dry_run:
            time.sleep(5)

    log(f"\n{'='*60}")
    log(f"Done. Total replies written: {total}")
    log("=" * 60)


if __name__ == "__main__":
    main()
