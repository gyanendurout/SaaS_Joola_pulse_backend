"""
JOOLA X / Twitter Scraper via Apify REST API
=============================================
Fetches the most recent tweets from @joolapickleball using
Apify's apidojo/tweet-scraper actor, then upserts into Supabase.

Uses the Apify REST API directly (avoids SDK Pydantic validation bugs
on newer actor pricing schemas).

USAGE
-----
  python scrape_x_latest.py                  # fetch last 100 tweets
  python scrape_x_latest.py --tweets 200     # fetch more
  python scrape_x_latest.py --dry-run        # fetch & print, no DB write

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

X_HANDLE    = "joolapickleball"
ACCOUNT_ID  = "fb5c25ea-30fb-48b2-a808-c08c87291c2b"
BRAND_ID    = "04db8591-37a3-4634-9d11-536975fa6935"

APIFY_BASE  = "https://api.apify.com/v2"
ACTOR_ID    = "apidojo~tweet-scraper"


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
        # Twitter format: "Thu Jul 25 23:47:46 +0000 2024"
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
    """Start an Apify actor run and return the run ID."""
    url = f"{APIFY_BASE}/acts/{ACTOR_ID}/runs"
    resp = requests.post(url, headers=_apify_headers(), json=run_input, timeout=30)
    if not resp.ok:
        print(f"  ERROR starting actor: {resp.status_code} — {resp.text[:500]}")
        sys.exit(1)
    run_id = resp.json()["data"]["id"]
    print(f"  Actor run started. Run ID: {run_id}")
    return run_id


def apify_wait_for_run(run_id: str, timeout_sec: int = 300) -> str:
    """Poll run status until SUCCEEDED/FAILED/ABORTED. Returns dataset ID."""
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
            print(f"  ERROR: Run {status}. Check https://console.apify.com/actors/runs/{run_id}")
            sys.exit(1)
        time.sleep(8)


def apify_fetch_items(dataset_id: str) -> list[dict]:
    """Download all items from an Apify dataset."""
    url = f"{APIFY_BASE}/datasets/{dataset_id}/items"
    params = {"clean": "true", "format": "json"}
    resp = requests.get(url, headers=_apify_headers(), params=params, timeout=60)
    if not resp.ok:
        print(f"  ERROR fetching dataset: {resp.status_code} — {resp.text[:300]}")
        sys.exit(1)
    return resp.json()


# ── Main ──────────────────────────────────────────────────────────────────────
def run_apify_scraper(max_tweets: int) -> list[dict]:
    print(f"\nStarting Apify tweet-scraper for @{X_HANDLE} (limit {max_tweets}) …")

    # apidojo/tweet-scraper input format
    run_input = {
        "searchTerms": [f"from:{X_HANDLE}"],
        "maxItems": max_tweets,
        "sort": "Latest",
        "includeSearchTerms": False,
        "onlyImage": False,
        "onlyQuote": False,
        "onlyTwitterBlue": False,
        "onlyVerifiedAccounts": False,
        "onlyVideo": False,
    }

    run_id = apify_start_run(run_input)
    dataset_id = apify_wait_for_run(run_id, timeout_sec=300)
    items = apify_fetch_items(dataset_id)
    print(f"  Got {len(items)} tweets from dataset.")
    return items


def main() -> None:
    parser = argparse.ArgumentParser(description=f"Scrape @{X_HANDLE} tweets → Supabase via Apify")
    parser.add_argument("--tweets",  type=int, default=100,  help="Max tweets to fetch (default 100)")
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

    items = run_apify_scraper(args.tweets)
    if not items:
        print("No items returned from Apify. Check actor run logs at console.apify.com.")
        sys.exit(1)

    post_rows: list[dict] = []

    for idx, item in enumerate(items, 1):
        # apidojo/tweet-scraper field names
        tweet_id = str(item.get("id") or item.get("tweet_id") or "")
        if not tweet_id:
            continue

        ts = _parse_ts(item.get("createdAt") or item.get("created_at") or item.get("date"))
        posted = datetime.fromisoformat(ts) if ts else None

        # Detect retweet from text prefix
        text_raw = (item.get("text") or item.get("full_text") or "")
        is_rt = text_raw.startswith("RT @") or bool(item.get("retweetedTweet"))
        post_type = "RT" if is_rt else "Original"

        like_count    = _safe_int(item.get("likeCount")    or item.get("favorite_count") or item.get("likes"))
        retweet_count = _safe_int(item.get("retweetCount") or item.get("retweet_count")  or item.get("retweets"))
        reply_count   = _safe_int(item.get("replyCount")   or item.get("reply_count")    or item.get("replies"))
        view_count    = _safe_int(item.get("viewCount")    or item.get("views")          or item.get("impressions"))

        # Build tweet URL
        author_handle = (item.get("author", {}) or {}).get("userName") or X_HANDLE
        post_url = item.get("url") or item.get("tweetUrl") or f"https://x.com/{author_handle}/status/{tweet_id}"

        text = text_raw[:2000] or None

        post_rows.append({
            "account_id":    ACCOUNT_ID,
            "brand_id":      BRAND_ID,
            "handle":        X_HANDLE,
            "tweet_id":      tweet_id,
            "post_url":      post_url,
            "text":          text,
            "like_count":    like_count,
            "retweet_count": retweet_count,
            "reply_count":   reply_count,
            "view_count":    view_count,
            "posted_at":     ts,
        })

        date_str = posted.strftime("%b %d, %Y") if posted else "unknown date"
        print(f"  [{idx:>3}/{len(items)}] {tweet_id}  likes={like_count}  rts={retweet_count}  views={view_count}  {date_str}  [{post_type}]")

    # Deduplicate by tweet_id (scraper sometimes returns duplicates)
    seen: set[str] = set()
    unique_rows: list[dict] = []
    for row in post_rows:
        tid = row["tweet_id"]
        if tid not in seen:
            seen.add(tid)
            unique_rows.append(row)
    post_rows = unique_rows

    print(f"\nSummary: {len(post_rows)} unique tweets ready to upsert")

    if args.dry_run:
        print("\n[DRY RUN] First tweet row:")
        print(json.dumps(post_rows[0] if post_rows else {}, indent=2, default=str))
        return

    print("\nWriting to Supabase …")
    sb_upsert("x_posts", post_rows, on_conflict="tweet_id")
    print("\nDone! Refresh the dashboard to see updated data.")
    print("Tip: run the AI enrichment pipeline next to add sentiment, topics, and flags.")


if __name__ == "__main__":
    main()
