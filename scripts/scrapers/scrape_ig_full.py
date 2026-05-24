"""JOOLA Pulse — full Instagram pipeline (posts → comments → AI analysis → derived tables).

Backfills posts & comments since the last scrape, runs OpenAI analysis,
and rebuilds all derived tables. Idempotent — re-running upserts by primary key.

For a lightweight quick-fetch without AI analysis, use scrape_ig.py instead.

Tables written:
  joola_ig_posts             (PK: post_id)
  joola_ig_comments          (PK: comment_id)
  joola_ig_comment_analysis  (PK: comment_id)
  joola_ig_post_analysis     (PK: post_id)
  joola_ig_loyal_users       (PK: username)
  joola_ig_complaint_log     (PK: comment_id)
  joola_ig_wishlist_items    (PK: comment_id)
  joola_ig_weekly_snapshot   (PK: week_start)

Apify actors:
  apify/instagram-profile-scraper
  apify/instagram-comment-scraper

Usage (from the backend directory, with venv activated):
    python scripts/scrapers/scrape_ig_full.py

Required env vars (in backend/.env):
  SUPABASE_URL  or  NEXT_PUBLIC_SUPABASE_URL   — Supabase project URL
  SUPABASE_SERVICE_ROLE_KEY                    — Supabase service role key
  APIFY_TOKEN   or  APIFY_API_TOKEN            — Apify API token
  OPENAI_API_KEY                               — OpenAI API key
"""

import os
import re
import sys
import time
import json as jsonlib
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from urllib.parse import quote

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Backend root is 3 levels up: scrapers/ → scripts/ → backend/
BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent

# Load env from backend/.env (falls back to frontend/.env.local for legacy compat)
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

# Accept both backend and legacy frontend env var names
APIFY_TOKEN  = os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
OPENAI_KEY   = os.environ.get("OPENAI_API_KEY", "")

if not all([APIFY_TOKEN, SUPABASE_URL, SUPABASE_KEY, OPENAI_KEY]):
    missing = [
        k for k, v in [
            ("APIFY_TOKEN / APIFY_API_TOKEN", APIFY_TOKEN),
            ("SUPABASE_URL / NEXT_PUBLIC_SUPABASE_URL", SUPABASE_URL),
            ("SUPABASE_SERVICE_ROLE_KEY", SUPABASE_KEY),
            ("OPENAI_API_KEY", OPENAI_KEY),
        ] if not v
    ]
    print(f"ERROR: Missing env vars: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)

JOOLA_HANDLE = "joolapickleball"
LOG_FILE   = BACKEND_ROOT / "logs" / "scrape_ig_full.log"
STATE_FILE = Path(__file__).parent / "scrape_ig_full.state.json"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

APIFY_BASE = "https://api.apify.com/v2"
SB_HEADERS = {
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

STATE: dict = {"step": "init", "posts_scraped": 0, "comments_scraped": 0, "comments_analyzed": 0}


def update_state(**kw: object) -> None:
    STATE.update(kw)
    STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        STATE_FILE.write_text(jsonlib.dumps(STATE, indent=2), encoding="utf-8")
    except Exception:
        pass


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ─── HTTP helpers ────────────────────────────────────────────────────────────


def http_get(url: str, headers: dict | None = None, timeout: int = 30) -> requests.Response:
    last_err: Exception | None = None
    for i in range(1, 6):
        try:
            return requests.get(url, headers=headers, timeout=timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_err = e
            log(f"  ⚠ GET retry {i}/5: {e}")
            time.sleep(10)
    raise RuntimeError(f"GET failed: {last_err}")


def http_post(url: str, headers: dict | None = None, json_data: object = None, timeout: int = 30) -> requests.Response:
    last_err: Exception | None = None
    for i in range(1, 6):
        try:
            return requests.post(url, headers=headers, json=json_data, timeout=timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_err = e
            log(f"  ⚠ POST retry {i}/5: {e}")
            time.sleep(10)
    raise RuntimeError(f"POST failed: {last_err}")


# ─── Apify ────────────────────────────────────────────────────────────────────


def run_actor(actor_id: str, input_data: dict) -> str:
    actor_url = actor_id.replace("/", "~")
    url = f"{APIFY_BASE}/acts/{actor_url}/runs?token={APIFY_TOKEN}"
    r = http_post(url, json_data=input_data, timeout=30)
    r.raise_for_status()
    run_id = r.json()["data"]["id"]
    log(f"  ▶ Started {actor_id} → run {run_id}")
    return run_id


def wait_for_run(run_id: str, poll: int = 15) -> bool:
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


def fetch_results(run_id: str) -> list:
    url = f"{APIFY_BASE}/actor-runs/{run_id}/dataset/items?token={APIFY_TOKEN}&clean=true"
    r = http_get(url, timeout=60)
    r.raise_for_status()
    return r.json()


# ─── Supabase ─────────────────────────────────────────────────────────────────


def sb_get_all(table: str, select: str, qs: str = "") -> list:
    out = []
    offset = 0
    while True:
        url = (
            f"{SUPABASE_URL}/rest/v1/{table}?select={select}"
            f"{('&' + qs) if qs else ''}&limit=1000&offset={offset}"
        )
        r = http_get(url, headers=SB_READ_HEADERS, timeout=30)
        r.raise_for_status()
        chunk = r.json()
        out.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return out


def sb_latest(table: str, col: str) -> str | None:
    url = f"{SUPABASE_URL}/rest/v1/{table}?select={col}&order={col}.desc.nullslast&limit=1"
    r = http_get(url, headers=SB_READ_HEADERS, timeout=15)
    r.raise_for_status()
    rows = r.json()
    return rows[0][col] if rows else None


def _replace_by_key(table: str, batch: list, key_col: str) -> bool:
    keys = [r[key_col] for r in batch if r.get(key_col) is not None]
    if keys:
        quoted = ",".join(f'"{k}"' for k in keys)
        del_url = f"{SUPABASE_URL}/rest/v1/{table}?{key_col}=in.({quoted})"
        dr = requests.delete(
            del_url,
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=30,
        )
        if dr.status_code not in (200, 204):
            log(f"  ✗ Delete {table}: {dr.status_code} {dr.text[:200]}")
            return False
    ins_url = f"{SUPABASE_URL}/rest/v1/{table}"
    ir = requests.post(
        ins_url,
        headers={**SB_HEADERS, "Prefer": "return=minimal"},
        json=batch,
        timeout=45,
    )
    if ir.status_code not in (200, 201, 204):
        log(f"  ✗ Insert {table}: {ir.status_code} {ir.text[:200]}")
        return False
    return True


def sb_upsert(table: str, rows: list, on_conflict: str, chunk: int = 200) -> int:
    if not rows:
        return 0
    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}"
    inserted = 0
    fallback = False
    for i in range(0, len(rows), chunk):
        batch = rows[i : i + chunk]
        if fallback:
            if _replace_by_key(table, batch, on_conflict):
                inserted += len(batch)
            continue
        r = http_post(url, headers=SB_HEADERS, json_data=batch, timeout=45)
        if r.status_code in (200, 201, 204):
            inserted += len(batch)
        elif r.status_code == 400 and "42P10" in r.text:
            log(f"  ⚠ {table}: no UNIQUE on {on_conflict} — switching to delete+insert")
            fallback = True
            if _replace_by_key(table, batch, on_conflict):
                inserted += len(batch)
        else:
            log(f"  ✗ Upsert {table} {r.status_code}: {r.text[:300]}")
    return inserted


# ─── Helpers ──────────────────────────────────────────────────────────────────


TYPE_MAP = {
    "Image": "image", "Video": "video", "Sidecar": "carousel",
    "GraphImage": "image", "GraphVideo": "video", "GraphSidecar": "carousel",
    "XDTMediaTypeVideo": "reel",
    "reel": "reel", "image": "image", "video": "video", "carousel": "carousel",
}


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def hashtags_of(text: str | None) -> list[str]:
    return re.findall(r"#(\w+)", text or "")


def mentions_of(text: str | None) -> list[str]:
    return re.findall(r"@(\w[\w.]*)", text or "")


def has_cta(text: str | None) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(k in t for k in (
        "link in bio", "shop now", "order now", "available", "buy now",
        "tap link", "dm us", "swipe up", "click", "comment below", "tag a"
    ))


def emoji_count(text: str | None) -> int:
    return sum(1 for c in (text or "") if ord(c) > 0x2000)


# ─── Step 1: posts ────────────────────────────────────────────────────────────


def scrape_posts(since_iso: str | None) -> list[dict]:
    log(f"\n[1/8] Scraping IG posts since posted_at > {since_iso}")
    update_state(step="posts")
    run_id = run_actor(
        "apify/instagram-profile-scraper",
        {"usernames": [JOOLA_HANDLE], "resultsLimit": 100},
    )
    if not wait_for_run(run_id):
        log("  ⚠ profile scraper failed — aborting")
        return []
    items = fetch_results(run_id)
    log(f"  fetched {len(items)} profile records")

    cutoff = parse_iso(since_iso)
    raw_posts = []
    for prof in items:
        lp = prof.get("latestPosts", []) or []
        tp = prof.get("topPosts", []) or []
        log(f"  profile @{prof.get('username')}: postsCount={prof.get('postsCount')} "
            f"followers={prof.get('followersCount')} latestPosts={len(lp)} topPosts={len(tp)}")
        raw_posts.extend(lp)
        raw_posts.extend(tp)

    seen: dict[str, dict] = {}
    for p in raw_posts:
        sc = p.get("shortCode") or p.get("id")
        if not sc:
            continue
        ts = p.get("timestamp")
        tsd = parse_iso(ts)
        if cutoff and tsd and tsd <= cutoff:
            continue
        caption = (p.get("caption") or "")[:5000]
        dow = tsd.strftime("%A").lower() if tsd else None
        hour = tsd.hour if tsd else None
        seen[sc] = {
            "post_id": sc,
            "post_url": f"https://www.instagram.com/p/{sc}/",
            "post_type": TYPE_MAP.get(p.get("type", ""), "image"),
            "caption": caption,
            "hashtags": hashtags_of(caption),
            "mentions": mentions_of(caption),
            "tagged_accounts": [],
            "has_cta": has_cta(caption),
            "cta_text": None,
            "caption_length": len(caption),
            "emoji_count": emoji_count(caption),
            "language": "en",
            "like_count": p.get("likesCount") or 0,
            "comment_count": p.get("commentsCount") or 0,
            "view_count": p.get("videoViewCount") or p.get("videoPlayCount") or 0,
            "carousel_slide_count": len(p.get("childPosts") or []) or None,
            "posted_at": ts,
            "day_of_week": dow,
            "hour_of_day": hour,
            "engagement_rate": None,
            "media_urls": [p.get("displayUrl")] if p.get("displayUrl") else [],
            "thumbnail_url": p.get("displayUrl"),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }

    new_posts = list(seen.values())
    log(f"  {len(new_posts)} new posts after filtering > {since_iso}")
    if new_posts:
        n = sb_upsert("joola_ig_posts", new_posts, "post_id")
        log(f"  ✓ upserted {n} into joola_ig_posts")
    update_state(posts_scraped=len(new_posts))
    return new_posts


# ─── Step 2: comments ─────────────────────────────────────────────────────────


def scrape_comments(new_posts: list[dict], comment_cutoff_iso: str | None) -> list[dict]:
    log(f"\n[2/8] Scraping comments on {len(new_posts)} new posts")
    update_state(step="comments")
    if not new_posts:
        return []
    urls = [p["post_url"] for p in new_posts]
    run_id = run_actor(
        "apify/instagram-comment-scraper",
        {"directUrls": urls, "resultsLimit": 80},
    )
    if not wait_for_run(run_id, poll=20):
        log("  ⚠ comment scraper failed")
        return []
    items = fetch_results(run_id)
    log(f"  fetched {len(items)} comments")

    cutoff = parse_iso(comment_cutoff_iso)
    url_to_post = {p["post_url"]: p for p in new_posts}

    out = []
    for c in items:
        cid = c.get("id") or c.get("commentId")
        if not cid:
            continue
        purl = (c.get("postUrl") or c.get("ownerPostUrl") or "").split("?")[0]
        post = url_to_post.get(purl) or url_to_post.get(purl.rstrip("/") + "/")
        if not post:
            m = re.search(r"/p/([^/?]+)", purl)
            post_id = m.group(1) if m else None
        else:
            post_id = post["post_id"]
        if not post_id:
            continue
        ts = c.get("timestamp")
        tsd = parse_iso(ts)
        if cutoff and tsd and tsd <= cutoff:
            continue
        text = (c.get("text") or "")[:3000]
        username = (c.get("ownerUsername") or c.get("username") or "").strip()
        if not username:
            continue
        out.append({
            "comment_id": cid,
            "post_id": post_id,
            "username": username,
            "comment_text": text,
            "comment_length": len(text),
            "language": "en",
            "emoji_count": emoji_count(text),
            "likes_on_comment": c.get("likesCount") or 0,
            "is_reply": bool(c.get("repliedToCommentId") or c.get("parentId")),
            "parent_comment_id": c.get("repliedToCommentId") or c.get("parentId"),
            "thread_depth": 1 if c.get("repliedToCommentId") else 0,
            "reply_count": c.get("repliesCount") or 0,
            "is_joola_reply": username.lower() in ("joola_pickleball", "joola"),
            "commented_at": ts,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })

    log(f"  {len(out)} new comments after filtering > {comment_cutoff_iso}")
    if out:
        n = sb_upsert("joola_ig_comments", out, "comment_id")
        log(f"  ✓ upserted {n} into joola_ig_comments")
    update_state(comments_scraped=len(out))
    return out


# ─── OpenAI ───────────────────────────────────────────────────────────────────


def openai_chat(messages: list[dict], model: str = "gpt-4o-mini", temperature: int = 0, response_format: dict | None = None) -> str | None:
    url = "https://api.openai.com/v1/chat/completions"
    h = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    body: dict = {"model": model, "messages": messages, "temperature": temperature}
    if response_format:
        body["response_format"] = response_format
    for i in range(1, 4):
        try:
            r = requests.post(url, headers=h, json=body, timeout=120)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            log(f"  ⚠ OpenAI net err {i}/3: {e}")
            time.sleep(10)
            continue
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        log(f"  ⚠ OpenAI retry {i}/3: {r.status_code} {r.text[:200]}")
        time.sleep(10)
    return None


# ─── Step 3: comment analysis ─────────────────────────────────────────────────


COMMENT_PROMPT = """You analyze JOOLA pickleball Instagram comments.
For each item in the input list, output a JSON object with EXACTLY these keys:
  sentiment: "positive"|"negative"|"neutral"
  sentiment_score: number in [-1, 1]
  emotion: "joy"|"anger"|"sadness"|"surprise"|"disgust"|"fear"|"neutral"
  primary_topic: "paddle"|"ball"|"tournament"|"athlete"|"service"|"delivery"|"durability"|"price"|"general"
  is_question: boolean
  is_complaint: boolean
  complaint_category: "durability"|"delivery"|"price"|"service"|"quality"|"other"|null
  is_wishlist: boolean
  wishlist_text: string|null
  mentions_competitor: boolean
  competitor_mentioned: string|null
  purchase_intent: boolean
  is_spam: boolean
Respond ONLY as JSON: {"results":[ ... same order as input ... ]}"""


def analyze_comments(comments: list[dict]) -> int:
    if not comments:
        log("\n[3/8] No new comments to analyze — skipping")
        return 0
    log(f"\n[3/8] Analyzing {len(comments)} comments via gpt-4o-mini")
    update_state(step="analyze_comments")
    out_buf: list[dict] = []
    flushed = 0
    BATCH = 10
    for i in range(0, len(comments), BATCH):
        batch = comments[i : i + BATCH]
        payload = [{"i": j, "text": c["comment_text"][:500]} for j, c in enumerate(batch)]
        content = openai_chat(
            [
                {"role": "system", "content": COMMENT_PROMPT},
                {"role": "user", "content": jsonlib.dumps(payload)},
            ],
            response_format={"type": "json_object"},
        )
        if not content:
            log(f"  ⚠ batch {i // BATCH + 1} skipped")
            continue
        try:
            results = jsonlib.loads(content).get("results", [])
        except Exception as e:
            log(f"  ⚠ parse err batch {i // BATCH + 1}: {e}")
            continue
        for k, res in enumerate(results):
            if k >= len(batch):
                break
            c = batch[k]
            out_buf.append({
                "comment_id": c["comment_id"],
                "post_id": c["post_id"],
                "username": c["username"],
                "sentiment": res.get("sentiment") or "neutral",
                "sentiment_score": res.get("sentiment_score"),
                "emotion": res.get("emotion") or "neutral",
                "primary_topic": res.get("primary_topic"),
                "secondary_topic": None,
                "is_question": bool(res.get("is_question")),
                "question_text": c["comment_text"] if res.get("is_question") else None,
                "is_complaint": bool(res.get("is_complaint")),
                "complaint_category": res.get("complaint_category"),
                "is_wishlist": bool(res.get("is_wishlist")),
                "wishlist_text": res.get("wishlist_text"),
                "mentions_competitor": bool(res.get("mentions_competitor")),
                "competitor_mentioned": res.get("competitor_mentioned"),
                "competitor_context": None,
                "purchase_intent": bool(res.get("purchase_intent")),
                "purchase_signal_text": c["comment_text"] if res.get("purchase_intent") else None,
                "product_mentioned": None,
                "athlete_mentioned": None,
                "is_spam": bool(res.get("is_spam")),
                "is_bot_likely": False,
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
            })
        if (i // BATCH + 1) % 4 == 0 and out_buf:
            n = sb_upsert("joola_ig_comment_analysis", out_buf, "comment_id")
            flushed += n
            log(f"  ✓ flushed {n} (total {flushed}/{len(comments)})")
            update_state(comments_analyzed=flushed)
            out_buf = []
    if out_buf:
        n = sb_upsert("joola_ig_comment_analysis", out_buf, "comment_id")
        flushed += n
        log(f"  ✓ final flush {n} (total {flushed}/{len(comments)})")
    update_state(comments_analyzed=flushed)
    return flushed


# ─── Step 4: post analysis ────────────────────────────────────────────────────


POST_PROMPT = """Analyze JOOLA Instagram posts. For each input post output JSON keys:
  content_theme: "athlete_spotlight"|"product_launch"|"tournament"|"tutorial"|"community"|"ugc"|"sale"|"general"
  post_intent: "announce"|"educate"|"sell"|"engage"|"celebrate"|"general"
  sentiment_tone: "positive"|"neutral"|"hype"|"emotional"|"informative"
  caption_summary: string (<= 20 words)
  caption_quality_score: integer 1-10
  hashtag_relevance_score: integer 1-10
  cta_type: "shop"|"learn"|"follow"|"comment"|"none"
  tournament_reference: string|null
  predicted_performance: "low"|"mid"|"high"
Respond ONLY: {"results":[ ... same order ... ]}"""


def analyze_posts(posts: list[dict]) -> int:
    if not posts:
        log("\n[4/8] No new posts to analyze — skipping")
        return 0
    log(f"\n[4/8] Analyzing {len(posts)} posts via gpt-4o-mini")
    update_state(step="analyze_posts")
    out: list[dict] = []
    BATCH = 5
    for i in range(0, len(posts), BATCH):
        batch = posts[i : i + BATCH]
        payload = [
            {
                "i": j,
                "caption": p["caption"][:800],
                "type": p["post_type"],
                "hashtags": p["hashtags"],
            }
            for j, p in enumerate(batch)
        ]
        content = openai_chat(
            [
                {"role": "system", "content": POST_PROMPT},
                {"role": "user", "content": jsonlib.dumps(payload)},
            ],
            response_format={"type": "json_object"},
        )
        if not content:
            continue
        try:
            results = jsonlib.loads(content).get("results", [])
        except Exception:
            continue
        for k, res in enumerate(results):
            if k >= len(batch):
                break
            p = batch[k]
            out.append({
                "post_id": p["post_id"],
                "content_theme": res.get("content_theme"),
                "content_subtheme": None,
                "products_shown": [],
                "athletes_shown": [],
                "setting": None,
                "shot_type": None,
                "has_text_overlay": False,
                "text_overlay_content": None,
                "brand_colors_present": True,
                "brand_logo_visible": True,
                "people_count": 0,
                "visual_quality_score": res.get("caption_quality_score"),
                "post_intent": res.get("post_intent"),
                "sentiment_tone": res.get("sentiment_tone"),
                "caption_summary": res.get("caption_summary"),
                "caption_quality_score": res.get("caption_quality_score"),
                "hashtag_count": len(p["hashtags"]),
                "hashtag_relevance_score": res.get("hashtag_relevance_score"),
                "cta_type": res.get("cta_type"),
                "is_sponsored": False,
                "sponsor_brand": None,
                "tournament_reference": res.get("tournament_reference"),
                "predicted_performance": res.get("predicted_performance"),
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
            })
    if out:
        n = sb_upsert("joola_ig_post_analysis", out, "post_id")
        log(f"  ✓ upserted {n} into joola_ig_post_analysis")
    return len(out)


# ─── Step 5: complaints + wishlist ────────────────────────────────────────────


def extract_complaints_wishlist(new_comments: list[dict]) -> None:
    log("\n[5/8] Extracting complaints + wishlist from new comments")
    update_state(step="complaints_wishlist")
    if not new_comments:
        log("  no new comments — skipping")
        return
    ids = [c["comment_id"] for c in new_comments]
    cmap = {c["comment_id"]: c for c in new_comments}
    analyses: dict[str, dict] = {}
    for i in range(0, len(ids), 80):
        chunk = ids[i : i + 80]
        q = ",".join(f'"{x}"' for x in chunk)
        url = f"{SUPABASE_URL}/rest/v1/joola_ig_comment_analysis?select=*&comment_id=in.({q})"
        r = http_get(url, headers=SB_READ_HEADERS, timeout=30)
        for a in r.json():
            analyses[a["comment_id"]] = a

    complaints: list[dict] = []
    wishlist: list[dict] = []
    for cid, a in analyses.items():
        c = cmap.get(cid)
        if not c:
            continue
        if a.get("is_complaint"):
            s = a.get("sentiment_score")
            sev = "low"
            if s is not None and s <= -0.6:
                sev = "high"
            elif s is not None and s <= -0.3:
                sev = "medium"
            complaints.append({
                "comment_id": cid,
                "post_id": c["post_id"],
                "username": c["username"],
                "complaint_category": a.get("complaint_category") or "other",
                "complaint_text": c["comment_text"],
                "severity": sev,
                "joola_responded": False,
                "joola_response_text": None,
                "joola_response_time_mins": None,
                "complained_at": c["commented_at"],
            })
        if a.get("is_wishlist"):
            wishlist.append({
                "comment_id": cid,
                "post_id": c["post_id"],
                "username": c["username"],
                "wishlist_text": a.get("wishlist_text") or c["comment_text"][:500],
                "category": "general",
                "product_reference": None,
                "request_summary": None,
                "times_similar_requested": 1,
                "requested_at": c["commented_at"],
            })
    n1 = sb_upsert("joola_ig_complaint_log", complaints, "comment_id") if complaints else 0
    log(f"  ✓ {n1} complaints stored")
    n2 = sb_upsert("joola_ig_wishlist_items", wishlist, "comment_id") if wishlist else 0
    log(f"  ✓ {n2} wishlist items stored")


# ─── Step 6: rebuild loyal users ─────────────────────────────────────────────


def rebuild_loyal_users() -> None:
    log("\n[6/8] Rebuilding joola_ig_loyal_users aggregates")
    update_state(step="loyal_users")
    log("  loading comments (paged)…")
    comments = sb_get_all(
        "joola_ig_comments",
        "comment_id,post_id,username,commented_at,likes_on_comment,is_joola_reply,is_reply",
    )
    log(f"  loaded {len(comments)} comments")

    log("  loading comment_analysis (paged)…")
    analysis_list = sb_get_all(
        "joola_ig_comment_analysis",
        "comment_id,sentiment_score,emotion,primary_topic,"
        "is_complaint,is_wishlist,is_question,purchase_intent,mentions_competitor",
    )
    analysis = {a["comment_id"]: a for a in analysis_list}
    log(f"  loaded {len(analysis)} analyses")

    agg: dict = defaultdict(lambda: {
        "total_comments": 0,
        "posts_set": set(),
        "replies": 0,
        "likes_recv": 0,
        "first_seen": None,
        "last_seen": None,
        "sent_scores": [],
        "emotions": defaultdict(int),
        "topics": defaultdict(int),
        "complaint": 0,
        "praise": 0,
        "question": 0,
        "purchase": 0,
        "competitor": 0,
        "wishlist": 0,
    })
    for c in comments:
        u = (c.get("username") or "").strip()
        if not u or u.lower() in ("joola_pickleball", "joola"):
            continue
        rec = agg[u]
        rec["total_comments"] += 1
        if c.get("post_id"):
            rec["posts_set"].add(c["post_id"])
        rec["likes_recv"] += c.get("likes_on_comment") or 0
        if c.get("is_reply"):
            rec["replies"] += 1
        ts = c.get("commented_at")
        if ts:
            if not rec["first_seen"] or ts < rec["first_seen"]:
                rec["first_seen"] = ts
            if not rec["last_seen"] or ts > rec["last_seen"]:
                rec["last_seen"] = ts
        a = analysis.get(c["comment_id"])
        if a:
            s = a.get("sentiment_score")
            if s is not None:
                rec["sent_scores"].append(float(s))
                if s > 0.4:
                    rec["praise"] += 1
            if a.get("emotion"):
                rec["emotions"][a["emotion"]] += 1
            if a.get("primary_topic"):
                rec["topics"][a["primary_topic"]] += 1
            if a.get("is_complaint"):
                rec["complaint"] += 1
            if a.get("is_wishlist"):
                rec["wishlist"] += 1
            if a.get("is_question"):
                rec["question"] += 1
            if a.get("purchase_intent"):
                rec["purchase"] += 1
            if a.get("mentions_competitor"):
                rec["competitor"] += 1

    rows = []
    for u, rec in agg.items():
        n = rec["total_comments"]
        avg_sent = sum(rec["sent_scores"]) / len(rec["sent_scores"]) if rec["sent_scores"] else 0.0
        dominant_emotion = max(rec["emotions"].items(), key=lambda kv: kv[1])[0] if rec["emotions"] else "neutral"
        dominant_topic = max(rec["topics"].items(), key=lambda kv: kv[1])[0] if rec["topics"] else "general"
        active_months = 0
        fs = parse_iso(rec["first_seen"])
        ls = parse_iso(rec["last_seen"])
        if fs and ls:
            active_months = max(1, (ls.year - fs.year) * 12 + (ls.month - fs.month) + 1)
        if n >= 50:
            tier = "super_fan"
        elif n >= 20:
            tier = "loyal"
        elif n >= 5:
            tier = "regular"
        else:
            tier = "casual"
        score = max(0, min(100, int(
            n * 0.5
            + len(rec["posts_set"]) * 1.0
            + max(0, avg_sent) * 20
            + rec["praise"] * 1.5
            - rec["complaint"] * 2.0
            - rec["competitor"] * 3.0
        )))
        ambassador = score >= 60 and avg_sent >= 0.3 and rec["complaint"] <= 1
        rows.append({
            "username": u,
            "display_name": None,
            "profile_url": f"https://www.instagram.com/{u}/",
            "bio": None,
            "follower_count": None,
            "following_count": None,
            "is_verified": False,
            "is_business_account": False,
            "is_potential_influencer": False,
            "total_comments": n,
            "total_posts_commented_on": len(rec["posts_set"]),
            "total_replies_made": rec["replies"],
            "total_likes_received_on_comments": rec["likes_recv"],
            "first_seen_at": rec["first_seen"],
            "last_seen_at": rec["last_seen"],
            "active_months": active_months,
            "avg_sentiment_score": avg_sent,
            "dominant_emotion": dominant_emotion,
            "dominant_topic": dominant_topic,
            "complaint_count": rec["complaint"],
            "praise_count": rec["praise"],
            "question_count": rec["question"],
            "purchase_intent_count": rec["purchase"],
            "competitor_mention_count": rec["competitor"],
            "wishlist_count": rec["wishlist"],
            "loyalty_tier": tier,
            "ambassador_score": score,
            "is_potential_ambassador": ambassador,
            "also_comments_on_competitors": rec["competitor"] > 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    if rows:
        n = sb_upsert("joola_ig_loyal_users", rows, "username", chunk=300)
        log(f"  ✓ upserted {n} loyal-user rows")


# ─── Step 7: weekly snapshots ─────────────────────────────────────────────────


def iso_week_start(dt: datetime | date) -> date:
    d = dt.date() if isinstance(dt, datetime) else dt
    return d - timedelta(days=d.weekday())


def rebuild_weekly_snapshots() -> None:
    log("\n[7/8] Rebuilding joola_ig_weekly_snapshot (last 14 weeks)")
    update_state(step="weekly")
    cutoff = (datetime.now(timezone.utc) - timedelta(weeks=14)).isoformat()
    cutoff_q = quote(cutoff, safe="")

    posts = sb_get_all(
        "joola_ig_posts",
        "post_id,posted_at,like_count,comment_count,view_count,engagement_rate",
        f"posted_at=gte.{cutoff_q}",
    )
    log(f"  loaded {len(posts)} recent posts")
    comments = sb_get_all(
        "joola_ig_comments",
        "comment_id,post_id,username,commented_at,is_joola_reply",
        f"commented_at=gte.{cutoff_q}",
    )
    log(f"  loaded {len(comments)} recent comments")
    analyses = sb_get_all(
        "joola_ig_comment_analysis",
        "comment_id,sentiment,sentiment_score,emotion,is_complaint,"
        "is_wishlist,purchase_intent,mentions_competitor",
    )
    analysis_idx = {a["comment_id"]: a for a in analyses}
    log(f"  loaded {len(analysis_idx)} analyses")

    by_week_posts: dict[date, list] = defaultdict(list)
    for p in posts:
        ts = parse_iso(p["posted_at"])
        if ts:
            by_week_posts[iso_week_start(ts)].append(p)

    by_week_comments: dict[date, list] = defaultdict(list)
    for c in comments:
        ts = parse_iso(c["commented_at"])
        if ts:
            by_week_comments[iso_week_start(ts)].append(c)

    seen_users_before: set[str] = set()
    rows = []
    weeks = sorted(set(list(by_week_posts.keys()) + list(by_week_comments.keys())))
    for ws in weeks:
        wp = by_week_posts.get(ws, [])
        wc = by_week_comments.get(ws, [])
        if not wp and not wc:
            continue
        total_likes = sum(p.get("like_count") or 0 for p in wp)
        total_comments_count = sum(p.get("comment_count") or 0 for p in wp) or len(wc)
        total_views = sum(p.get("view_count") or 0 for p in wp)
        avg_er = sum(p.get("engagement_rate") or 0 for p in wp) / len(wp) if wp else 0
        top_post = max(wp, key=lambda x: x.get("like_count") or 0, default=None)

        pos = neg = neu = 0
        emotions: dict[str, int] = defaultdict(int)
        sent_total = 0.0
        sent_n = 0
        complaints = purchase = competitor = wishlist = joola_reply = 0
        usernames_this_week: set[str] = set()
        for c in wc:
            usernames_this_week.add(c["username"])
            if c.get("is_joola_reply"):
                joola_reply += 1
            a = analysis_idx.get(c["comment_id"])
            if not a:
                continue
            s = a.get("sentiment")
            if s == "positive":
                pos += 1
            elif s == "negative":
                neg += 1
            else:
                neu += 1
            ss = a.get("sentiment_score")
            if ss is not None:
                sent_total += float(ss)
                sent_n += 1
            if a.get("emotion"):
                emotions[a["emotion"]] += 1
            if a.get("is_complaint"):
                complaints += 1
            if a.get("is_wishlist"):
                wishlist += 1
            if a.get("purchase_intent"):
                purchase += 1
            if a.get("mentions_competitor"):
                competitor += 1
        new_commenters = len(usernames_this_week - seen_users_before)
        returning = len(usernames_this_week & seen_users_before)
        seen_users_before |= usernames_this_week
        total = pos + neg + neu or 1
        top_emo = max(emotions.items(), key=lambda kv: kv[1])[0] if emotions else "neutral"
        rows.append({
            "week_start": ws.isoformat(),
            "week_end": (ws + timedelta(days=6)).isoformat(),
            "posts_published": len(wp),
            "total_likes": total_likes,
            "total_comments": total_comments_count,
            "total_views": total_views,
            "avg_engagement_rate": avg_er,
            "top_post_id": top_post["post_id"] if top_post else None,
            "top_post_engagement": (
                (top_post.get("like_count") or 0) + (top_post.get("comment_count") or 0)
                if top_post else 0
            ),
            "new_commenters": new_commenters,
            "returning_commenters": returning,
            "positive_comment_pct": pos * 100 / total,
            "negative_comment_pct": neg * 100 / total,
            "neutral_comment_pct": neu * 100 / total,
            "avg_sentiment_score": sent_total / sent_n if sent_n else 0,
            "top_emotion": top_emo,
            "complaint_count": complaints,
            "purchase_intent_count": purchase,
            "competitor_mention_count": competitor,
            "wishlist_count": wishlist,
            "joola_reply_count": joola_reply,
            "avg_joola_response_time_mins": None,
            "dominant_content_theme": None,
            "new_super_fans": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    if rows:
        n = sb_upsert("joola_ig_weekly_snapshot", rows, "week_start")
        log(f"  ✓ upserted {n} weekly snapshots")


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    LOG_FILE.write_text("", encoding="utf-8")
    log("=" * 60)
    log("JOOLA Pulse — Instagram full pipeline")
    log(f"Today: {date.today().isoformat()}  Handle: @{JOOLA_HANDLE}")
    log("=" * 60)

    log("\nReading DB cutoffs…")
    posts_cutoff = sb_latest("joola_ig_posts", "posted_at")
    comments_cutoff = sb_latest("joola_ig_comments", "commented_at")
    log(f"  latest posted_at    : {posts_cutoff}")
    log(f"  latest commented_at : {comments_cutoff}")
    update_state(posts_cutoff=posts_cutoff, comments_cutoff=comments_cutoff)

    new_posts = scrape_posts(posts_cutoff)
    new_comments = scrape_comments(new_posts, comments_cutoff)

    # Gap-fill: pick up rows scraped in earlier runs that are still missing analysis
    log("\n[2b/8] Checking for previously-scraped rows missing analysis…")
    gap_cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    gap_cutoff_q = quote(gap_cutoff_iso, safe="")

    analyzed_comment_ids = {a["comment_id"] for a in sb_get_all("joola_ig_comment_analysis", "comment_id")}
    recent_comments = sb_get_all(
        "joola_ig_comments",
        "comment_id,post_id,username,comment_text,commented_at",
        f"commented_at=gte.{gap_cutoff_q}",
    )
    gap_comments = [c for c in recent_comments if c["comment_id"] not in analyzed_comment_ids]
    log(f"  comments missing analysis: {len(gap_comments)}")
    merged_c = {c["comment_id"]: c for c in new_comments}
    for c in gap_comments:
        merged_c.setdefault(c["comment_id"], c)
    new_comments = list(merged_c.values())

    analyzed_post_ids = {a["post_id"] for a in sb_get_all("joola_ig_post_analysis", "post_id")}
    recent_posts_rows = sb_get_all(
        "joola_ig_posts",
        "post_id,caption,post_type,hashtags,posted_at",
        f"posted_at=gte.{gap_cutoff_q}",
    )
    gap_posts = [p for p in recent_posts_rows if p["post_id"] not in analyzed_post_ids]
    log(f"  posts missing analysis: {len(gap_posts)}")
    merged_p = {p["post_id"]: p for p in new_posts}
    for p in gap_posts:
        merged_p.setdefault(p["post_id"], {
            "post_id": p["post_id"],
            "caption": p.get("caption") or "",
            "post_type": p.get("post_type") or "image",
            "hashtags": p.get("hashtags") or [],
        })
    new_posts = list(merged_p.values())
    log(f"  → analyze queue: {len(new_comments)} comments, {len(new_posts)} posts")

    analyze_comments(new_comments)
    analyze_posts(new_posts)
    extract_complaints_wishlist(new_comments)
    rebuild_loyal_users()
    rebuild_weekly_snapshots()

    log("\n[8/8] Done")
    log("=" * 60)
    log("SCRAPE COMPLETE")
    log(f"  posts scraped     : {STATE.get('posts_scraped')}")
    log(f"  comments scraped  : {STATE.get('comments_scraped')}")
    log(f"  comments analyzed : {STATE.get('comments_analyzed')}")
    log("=" * 60)
    update_state(step="done", finished_at=datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"\n✗ FATAL: {type(e).__name__}: {e}")
        update_state(step="error", error=str(e))
        raise
