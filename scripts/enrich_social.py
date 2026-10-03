"""JOOLA Pulse — Social media AI enrichment for TikTok, X/Twitter, and Reddit.

Reads un-enriched rows (where enriched_at IS NULL for TikTok/X, or
sentiment IS NULL for Reddit), batches them through gpt-4o-mini, and
writes sentiment/topics/crisis signals back.

Tables written:
  tiktok_videos   — sentiment_label, sentiment_score, topics, brands_mentioned,
                    players_mentioned, products_mentioned, is_crisis,
                    is_opportunity, purchase_intent_score, crisis_keywords, enriched_at
  tiktok_comments — sentiment_label, sentiment_score, topics, is_crisis, is_opportunity
  x_posts         — same fields as tiktok_videos
  reddit_mentions — sentiment, topics, brands_mentioned, players_mentioned,
                    products_mentioned, is_crisis, is_opportunity, enriched_at
                    (note: is_crisis/is_opportunity already partially populated;
                     this pass fills the remaining nulls and adds sentiment)

Usage (from backend/ with venv activated):
    python scripts/enrich_social.py                             # all platforms
    python scripts/enrich_social.py --platform tiktok
    python scripts/enrich_social.py --platform tiktok_comments
    python scripts/enrich_social.py --platform twitter
    python scripts/enrich_social.py --platform reddit
    python scripts/enrich_social.py --dry-run                   # preview, no writes
    python scripts/enrich_social.py --limit 20                  # first N rows only

Required env vars (backend/.env):
    SUPABASE_URL  or  NEXT_PUBLIC_SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
    OPENAI_API_KEY
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

BACKEND_ROOT = Path(__file__).resolve().parent.parent


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

SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
OPENAI_KEY   = os.environ.get("OPENAI_API_KEY", "")

if not all([SUPABASE_URL, SUPABASE_KEY, OPENAI_KEY]):
    missing = [
        k for k, v in [
            ("SUPABASE_URL / NEXT_PUBLIC_SUPABASE_URL", SUPABASE_URL),
            ("SUPABASE_SERVICE_ROLE_KEY", SUPABASE_KEY),
            ("OPENAI_API_KEY", OPENAI_KEY),
        ] if not v
    ]
    print(f"ERROR: Missing env vars: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)

LOG_FILE = BACKEND_ROOT / "logs" / "enrich_social.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

SB_WRITE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
    "User-Agent": "joola-pulse-enricher/1.0",
}
SB_READ_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "User-Agent": "joola-pulse-enricher/1.0",
}

# ─── Domain constants ─────────────────────────────────────────────────────────

JOOLA_PLAYERS = [
    "ben johns", "anna leigh waters", "collin johns", "anna bright",
    "tyson mcguffin", "jay devilliers", "Parris todd", "lea jansen",
    "simone jardim", "zane navratil", "jorrit de waard", "jesse irvin",
    "altaf merchant", "irina tereschenko", "johns brothers", "waters family",
]

COMPETITOR_BRANDS = [
    "selkirk", "engage", "paddletek", "franklin", "crbn", "head",
    "wilson", "gamma", "prokennex", "six zero", "gearbox",
]

JOOLA_PRODUCTS = [
    "perseus", "scorpeus", "hyperion", "agassi", "magnus", "ben johns paddle",
    "anna leigh paddle", "swift", "vision", "collin johns", "journey", "solaire",
    "joola ball", "trifecta",
]

CRISIS_KEYWORDS = [
    "recall", "banned", "broke", "cracked", "delaminate", "lawsuit",
    "fraud", "fake", "scam", "dangerous", "injury", "cheat", "controversy",
]

# ─── Logging ──────────────────────────────────────────────────────────────────


def log(msg: str, dry_run: bool = False) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {prefix}{msg}"
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


def http_post(url: str, headers: dict | None = None, json_data: object = None, timeout: int = 60) -> requests.Response:
    for attempt in range(1, 6):
        try:
            return requests.post(url, headers=headers, json=json_data, timeout=timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            log(f"  ⚠ POST retry {attempt}/5: {e}")
            time.sleep(10)
    raise RuntimeError("POST failed after 5 retries")


# ─── Supabase helpers ─────────────────────────────────────────────────────────


def sb_get_all(table: str, select: str, qs: str = "", limit: int | None = None) -> list:
    out: list = []
    offset = 0
    page_size = 1000
    while True:
        url = (
            f"{SUPABASE_URL}/rest/v1/{table}?select={select}"
            f"{('&' + qs) if qs else ''}&limit={page_size}&offset={offset}"
        )
        r = http_get(url, headers=SB_READ_HEADERS)
        r.raise_for_status()
        chunk = r.json()
        out.extend(chunk)
        if len(chunk) < page_size:
            break
        offset += page_size
        if limit and len(out) >= limit:
            break
    return out[:limit] if limit else out


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
        r = http_post(url, headers=SB_WRITE_HEADERS, json_data=batch)
        if r.status_code in (200, 201, 204):
            written += len(batch)
        else:
            log(f"  ✗ Upsert {table}: {r.status_code} {r.text[:300]}")
    return written


# ─── OpenAI ───────────────────────────────────────────────────────────────────


def openai_chat(messages: list[dict], temperature: int = 0) -> str | None:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    body = {
        "model": "gpt-4o-mini",
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    for attempt in range(1, 4):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=120)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            log(f"  ⚠ OpenAI net err {attempt}/3: {e}")
            time.sleep(10)
            continue
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        log(f"  ⚠ OpenAI retry {attempt}/3: {r.status_code} {r.text[:200]}")
        time.sleep(10)
    return None


# ─── Enrichment prompt ────────────────────────────────────────────────────────

SOCIAL_PROMPT = f"""You analyze JOOLA pickleball social media posts (TikTok captions, tweets, Reddit posts).
JOOLA is a pickleball paddle/equipment brand. These are brand-authored posts.

Known JOOLA players: {', '.join(JOOLA_PLAYERS[:10])} and others.
Competitor brands: {', '.join(COMPETITOR_BRANDS)}.
JOOLA products: {', '.join(JOOLA_PRODUCTS[:8])} and others.
Crisis keywords (flag is_crisis=true if present): {', '.join(CRISIS_KEYWORDS)}.

For EACH item in the input list, output a JSON object with EXACTLY these keys:
  sentiment_label: "positive"|"negative"|"neutral"
  sentiment_score: float in [-1.0, 1.0]
  topics: array of strings (max 5, from: "athlete_spotlight","product_review","tournament",
          "tutorial","community","complaint","purchase_intent","competitor","general","media_coverage")
  brands_mentioned: array of brand name strings found in the text (empty array if none)
  players_mentioned: array of player name strings found in the text (empty array if none)
  products_mentioned: array of product name strings found in the text (empty array if none)
  is_crisis: boolean (true if text contains crisis signals: safety issues, recalls, controversies, major complaints)
  is_opportunity: boolean (true if text signals purchase intent, partnership potential, or positive brand moment)
  purchase_intent_score: float in [0.0, 1.0] (0=no intent, 1=strong intent to buy)
  crisis_keywords: array of crisis-related words actually found in the text (empty array if none)

Respond ONLY as JSON: {{"results": [ ...same order as input... ]}}"""

COMMENT_PROMPT = f"""You analyze user comments on JOOLA pickleball TikTok videos.
JOOLA is a pickleball paddle/equipment brand. These are fan/consumer comments, not brand posts.

Known JOOLA players: {', '.join(JOOLA_PLAYERS[:10])} and others.
Competitor brands: {', '.join(COMPETITOR_BRANDS)}.
Crisis signals (flag is_crisis=true): {', '.join(CRISIS_KEYWORDS)}.

For EACH item in the input list, output a JSON object with EXACTLY these keys:
  sentiment_label: "positive"|"negative"|"neutral"
  sentiment_score: float in [-1.0, 1.0]
  topics: array of strings (max 3, from: "product_review","complaint","purchase_intent",
          "athlete_spotlight","community","tutorial","competitor","general")
  is_crisis: boolean (true if comment signals safety issue, defect, controversy, or strong complaint)
  is_opportunity: boolean (true if comment signals purchase intent or positive brand engagement)

Respond ONLY as JSON: {{"results": [ ...same order as input... ]}}"""


# ─── Platform-specific fetchers ───────────────────────────────────────────────


def fetch_tiktok(limit: int | None) -> list[dict]:
    rows = sb_get_all(
        "tiktok_videos",
        "id,tiktok_video_id,text",
        "enriched_at=is.null&order=posted_at.desc",
        limit=limit,
    )
    log(f"  Found {len(rows)} TikTok videos to enrich")
    return rows


def fetch_tiktok_comments(limit: int | None) -> list[dict]:
    rows = sb_get_all(
        "tiktok_comments",
        "id,tiktok_comment_id,comment_text",
        "sentiment_label=is.null&order=posted_at.desc",
        limit=limit,
    )
    log(f"  Found {len(rows)} TikTok comments to enrich")
    return rows


def fetch_twitter(limit: int | None) -> list[dict]:
    rows = sb_get_all(
        "x_posts",
        "id,tweet_id,text",
        "enriched_at=is.null&order=posted_at.desc",
        limit=limit,
    )
    log(f"  Found {len(rows)} X/Twitter posts to enrich")
    return rows


def fetch_reddit(limit: int | None) -> list[dict]:
    rows = sb_get_all(
        "reddit_mentions",
        "id,reddit_post_id,post_title,content_text",
        "sentiment=is.null&order=posted_at.desc",
        limit=limit,
    )
    log(f"  Found {len(rows)} Reddit mentions to enrich (sentiment=null)")
    return rows


def _text_for_row(row: dict, platform: str) -> str:
    if platform == "reddit":
        title = row.get("post_title") or ""
        body  = row.get("content_text") or ""
        combined = f"{title}. {body}".strip(". ")
        return combined[:800]
    if platform == "tiktok_comments":
        return (row.get("comment_text") or "")[:500]
    return (row.get("text") or "")[:600]


def _pk_for_row(row: dict, platform: str) -> str:
    # Always use the UUID primary key (id) — the natural keys (tiktok_video_id,
    # tweet_id, reddit_post_id) lack UNIQUE constraints so on_conflict won't work.
    return row.get("id")


def _pk_col(platform: str) -> str:
    return "id"


def _table(platform: str) -> str:
    return {
        "tiktok":          "tiktok_videos",
        "tiktok_comments": "tiktok_comments",
        "twitter":         "x_posts",
        "reddit":          "reddit_mentions",
    }[platform]


# ─── Core enrichment ─────────────────────────────────────────────────────────


def enrich_platform(platform: str, rows: list[dict], dry_run: bool) -> int:
    if not rows:
        return 0

    table  = _table(platform)
    pk_col = _pk_col(platform)
    BATCH  = 10
    out_buf: list[dict] = []
    total_written = 0
    now = datetime.now(timezone.utc).isoformat()

    system_prompt = COMMENT_PROMPT if platform == "tiktok_comments" else SOCIAL_PROMPT
    log(f"\n  Enriching {len(rows)} {platform} rows via gpt-4o-mini (batch={BATCH})")

    for i in range(0, len(rows), BATCH):
        batch = rows[i: i + BATCH]
        payload = [
            {"i": j, "text": _text_for_row(r, platform)}
            for j, r in enumerate(batch)
        ]
        content = openai_chat([
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": jsonlib.dumps(payload)},
        ])

        if not content:
            log(f"  ⚠ batch {i // BATCH + 1} skipped (no response)")
            continue

        try:
            results = jsonlib.loads(content).get("results", [])
        except Exception as e:
            log(f"  ⚠ parse error batch {i // BATCH + 1}: {e}")
            continue

        for k, res in enumerate(results):
            if k >= len(batch):
                break
            row = batch[k]
            pk_val = _pk_for_row(row, platform)
            if not pk_val:
                continue

            label = res.get("sentiment_label") or "neutral"

            # tiktok_comments has a slim schema — only 5 enrichment columns
            if platform == "tiktok_comments":
                enriched: dict = {
                    pk_col:            pk_val,
                    "sentiment_label": label,
                    "sentiment_score": res.get("sentiment_score"),
                    "topics":          res.get("topics") or [],
                    "is_crisis":       bool(res.get("is_crisis")),
                    "is_opportunity":  bool(res.get("is_opportunity")),
                }
            else:
                enriched = {
                    pk_col:               pk_val,
                    "sentiment_label":    label,
                    "sentiment_score":    res.get("sentiment_score"),
                    "topics":             res.get("topics") or [],
                    "brands_mentioned":   res.get("brands_mentioned") or [],
                    "players_mentioned":  res.get("players_mentioned") or [],
                    "products_mentioned": res.get("products_mentioned") or [],
                    "is_crisis":          bool(res.get("is_crisis")),
                    "is_opportunity":     bool(res.get("is_opportunity")),
                    "purchase_intent_score": res.get("purchase_intent_score"),
                    "crisis_keywords":    res.get("crisis_keywords") or [],
                    "enriched_at":        now,
                }

            # Reddit has both "sentiment" and "sentiment_label" columns
            if platform == "reddit":
                enriched["sentiment"] = label

            out_buf.append(enriched)

        # Flush every 4 batches
        if (i // BATCH + 1) % 4 == 0 and out_buf:
            n = sb_upsert(table, out_buf, pk_col, dry_run=dry_run)
            total_written += n
            log(f"  ✓ flushed {n} (total {total_written}/{len(rows)})", dry_run=dry_run)
            out_buf = []

    # Final flush
    if out_buf:
        n = sb_upsert(table, out_buf, pk_col, dry_run=dry_run)
        total_written += n
        log(f"  ✓ final flush {n} (total {total_written}/{len(rows)})", dry_run=dry_run)

    return total_written


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich JOOLA social media data with AI signals")
    parser.add_argument(
        "--platform",
        choices=["tiktok", "tiktok_comments", "twitter", "reddit", "all"],
        default="all",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview only — no DB writes")
    parser.add_argument("--limit", type=int, default=None, help="Max rows per platform (testing)")
    args = parser.parse_args()

    platforms = ["tiktok", "tiktok_comments", "twitter", "reddit"] if args.platform == "all" else [args.platform]

    log("=" * 60)
    log(f"JOOLA Social Enrichment  platform={args.platform}  dry_run={args.dry_run}")
    if args.limit:
        log(f"  Limit: {args.limit} rows per platform")
    log("=" * 60)

    fetchers = {
        "tiktok":          fetch_tiktok,
        "tiktok_comments": fetch_tiktok_comments,
        "twitter":         fetch_twitter,
        "reddit":          fetch_reddit,
    }

    grand_total = 0
    for platform in platforms:
        log(f"\n[{platform.upper()}]")
        rows = fetchers[platform](args.limit)
        if not rows:
            log(f"  Nothing to enrich — already up to date")
            continue
        written = enrich_platform(platform, rows, dry_run=args.dry_run)
        log(f"  Done: {written}/{len(rows)} rows {'(dry-run)' if args.dry_run else 'written'}")
        grand_total += written

    log(f"\n{'='*60}")
    log(f"Grand total: {grand_total} rows {'previewed' if args.dry_run else 'enriched'}")
    log("=" * 60)


if __name__ == "__main__":
    main()
