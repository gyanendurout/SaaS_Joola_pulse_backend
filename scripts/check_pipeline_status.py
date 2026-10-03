"""
Pipeline Status Checker -- cross-verifies today's scrape freshness and enrichment gaps.
Usage: python scripts/check_pipeline_status.py
"""
# Force UTF-8 output on Windows terminals
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import os
from datetime import datetime, timezone
from pathlib import Path

# Load .env from backend root
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set")
    sys.exit(1)

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

TODAY = datetime.now(timezone.utc).date().isoformat()
TODAY_START = f"{TODAY}T00:00:00+00:00"

G = "\033[92m"   # green
Y = "\033[93m"   # yellow
R = "\033[91m"   # red
B = "\033[1m"    # bold
X = "\033[0m"    # reset


def ok(msg):   print(f"  {G}[OK]  {X}{msg}")
def warn(msg): print(f"  {Y}[WARN]{X} {msg}")
def bad(msg):  print(f"  {R}[FAIL]{X} {msg}")


def count_total(table):
    try:
        r = sb.table(table).select("id", count="exact").limit(0).execute()
        return r.count or 0
    except Exception as e:
        return f"ERR({e})"


def count_gte(table, col, val):
    try:
        r = sb.table(table).select("id", count="exact").gte(col, val).limit(0).execute()
        return r.count or 0
    except Exception as e:
        return f"ERR({e})"


def count_null(table, col):
    try:
        r = sb.table(table).select("id", count="exact").is_(col, "null").limit(0).execute()
        return r.count or 0
    except Exception as e:
        return f"ERR({e})"


def fresh(today_n, total):
    if isinstance(today_n, str): return f"{R}{today_n}{X}"
    if today_n > 0: return f"{G}{today_n} new today{X} (total: {total})"
    return f"{R}0 new today{X} (total: {total})"


def gap(null_n, total):
    if isinstance(null_n, str): return f"{R}{null_n}{X}"
    if isinstance(total, str):  return f"{R}total ERR{X}"
    if total == 0: return f"{Y}no rows{X}"
    pct = round(100 * null_n / total)
    if null_n == 0:   return f"{G}fully enriched{X}"
    if pct < 20:      return f"{Y}{null_n}/{total} unenriched ({pct}%){X}"
    return f"{R}{null_n}/{total} unenriched ({pct}%){X}"


# ======================================================
print(f"\n{B}{'='*60}{X}")
print(f"{B}  JOOLA Pulse -- Pipeline Status Check{X}")
print(f"  Date checked : {TODAY}")
print(f"{B}{'='*60}{X}\n")

# ── INSTAGRAM ─────────────────────────────────────────
print(f"{B}[ INSTAGRAM ]{X}")

ig_total = count_total("joola_ig_posts")
ig_today = count_gte("joola_ig_posts", "scraped_at", TODAY_START)
print(f"  joola_ig_posts             {fresh(ig_today, ig_total)}")

ipa_total = count_total("joola_ig_post_analysis")
ipa_null  = count_null("joola_ig_post_analysis", "content_theme")
print(f"  joola_ig_post_analysis     {gap(ipa_null, ipa_total)}  (posts: {ig_total})")

ica_total = count_total("joola_ig_comment_analysis")
ica_null  = count_null("joola_ig_comment_analysis", "sentiment")
print(f"  joola_ig_comment_analysis  {gap(ica_null, ica_total)}")

lu_total = count_total("joola_ig_loyal_users")
print(f"  joola_ig_loyal_users       {lu_total} ambassador candidates")

cl_total = count_total("joola_ig_complaint_log")
print(f"  joola_ig_complaint_log     {cl_total} complaints logged")

snap_wk   = count_gte("joola_ig_weekly_snapshot", "week_start", "2026-06-22")
snap_null = count_null("joola_ig_weekly_snapshot", "dominant_content_theme")
snap_msg  = f"{G}this-week row exists{X}" if isinstance(snap_wk, int) and snap_wk > 0 else f"{R}this-week row MISSING{X}"
print(f"  joola_ig_weekly_snapshot   {snap_msg} | dominant_theme nulls: {snap_null}")

# ── TIKTOK ────────────────────────────────────────────
print(f"\n{B}[ TIKTOK ]{X}")

tt_total    = count_total("tiktok_videos")
tt_today    = count_gte("tiktok_videos", "created_at", TODAY_START)
tt_enr_null = count_null("tiktok_videos", "enriched_at")
tt_sent_null= count_null("tiktok_videos", "sentiment_label")
print(f"  tiktok_videos      {fresh(tt_today, tt_total)}")
print(f"    enriched_at      {gap(tt_enr_null, tt_total)}")
print(f"    sentiment_label  {gap(tt_sent_null, tt_total)}")

tc_total    = count_total("tiktok_comments")
tc_today    = count_gte("tiktok_comments", "scraped_at", TODAY_START)
tc_sent_null= count_null("tiktok_comments", "sentiment_label")
print(f"  tiktok_comments    {fresh(tc_today, tc_total)}")
print(f"    sentiment_label  {gap(tc_sent_null, tc_total)}")

# ── X / TWITTER ───────────────────────────────────────
print(f"\n{B}[ X / TWITTER ]{X}")

xp_total    = count_total("x_posts")
xp_today    = count_gte("x_posts", "created_at", TODAY_START)
xp_enr_null = count_null("x_posts", "enriched_at")
xp_sent_null= count_null("x_posts", "sentiment_label")
print(f"  x_posts            {fresh(xp_today, xp_total)}")
print(f"    enriched_at      {gap(xp_enr_null, xp_total)}")
print(f"    sentiment_label  {gap(xp_sent_null, xp_total)}")

xr_total    = count_total("x_replies")
xr_today    = count_gte("x_replies", "scraped_at", TODAY_START)
xr_sent_null= count_null("x_replies", "sentiment_label")
print(f"  x_replies          {fresh(xr_today, xr_total)}")
print(f"    sentiment_label  {gap(xr_sent_null, xr_total)}")

# ── REDDIT ────────────────────────────────────────────
print(f"\n{B}[ REDDIT ]{X}")

rd_total    = count_total("reddit_mentions")
rd_today    = count_gte("reddit_mentions", "scraped_at", TODAY_START)
rd_sent_null= count_null("reddit_mentions", "sentiment")
rd_enr_null = count_null("reddit_mentions", "enriched_at")
rd_top_null = count_null("reddit_mentions", "topics")
print(f"  reddit_mentions    {fresh(rd_today, rd_total)}")
print(f"    sentiment        {gap(rd_sent_null, rd_total)}")
print(f"    enriched_at      {gap(rd_enr_null, rd_total)}")
print(f"    topics           {gap(rd_top_null, rd_total)}")

# ── YOUTUBE ───────────────────────────────────────────
print(f"\n{B}[ YOUTUBE ]{X}")

yt_total = count_total("yt_videos")
yt_today = count_gte("yt_videos", "published_at", TODAY_START)
print(f"  yt_videos          {fresh(yt_today, yt_total)}  (published_at = upload date)")

ytw_total = count_total("yt_channel_weekly")
ytw_today = count_gte("yt_channel_weekly", "scraped_at", TODAY_START)
print(f"  yt_channel_weekly  {fresh(ytw_today, ytw_total)}")

# ── INFLUENCERS ───────────────────────────────────────
print(f"\n{B}[ INFLUENCERS ]{X}")

inf_total    = count_total("influencer_posts")
inf_today    = count_gte("influencer_posts", "scraped_at", TODAY_START)
inf_sent_null= count_null("influencer_posts", "sentiment")
print(f"  influencer_posts   {fresh(inf_today, inf_total)}")
print(f"    sentiment        {gap(inf_sent_null, inf_total)}")

# ── NEWS ──────────────────────────────────────────────
print(f"\n{B}[ NEWS INTELLIGENCE ]{X}")

na_total    = count_total("news_articles")
na_today    = count_gte("news_articles", "scraped_at", TODAY_START)
na_sum_null = count_null("news_articles", "ai_summary")
na_sent_null= count_null("news_articles", "sentiment")
print(f"  news_articles      {fresh(na_today, na_total)}")
print(f"    ai_summary       {gap(na_sum_null, na_total)}")
print(f"    sentiment        {gap(na_sent_null, na_total)}")

try:
    r = sb.table("news_scrape_runs").select("status,started_at,articles_new").order("created_at", desc=True).limit(1).execute()
    if r.data:
        run = r.data[0]
        st  = run.get("status", "?")
        col = G if st == "done" else (R if st == "failed" else Y)
        print(f"  last news run      {col}{st}{X} | started: {(run.get('started_at') or '')[:19]} | new articles: {run.get('articles_new','?')}")
    else:
        warn("news_scrape_runs -- no runs found")
except Exception as e:
    bad(f"news_scrape_runs -- {e}")

# ── SEO ───────────────────────────────────────────────
print(f"\n{B}[ SEO ]{X}")

try:
    r = sb.table("runs").select("id,status,started_at").order("started_at", desc=True).limit(1).execute()
    if r.data:
        run = r.data[0]
        st  = run.get("status", "?")
        col = G if st == "done" else (R if st == "failed" else Y)
        rd  = (run.get("started_at") or "?")[:10]
        try:
            age = (datetime.fromisoformat(TODAY) - datetime.fromisoformat(rd)).days
            age_s = f"{age}d ago"
        except Exception:
            age_s = "?"
        print(f"  last SEO run       {col}{st}{X} | date: {rd} | age: {age_s}")
        if isinstance(age_s, str) and age_s.endswith("d ago"):
            days = int(age_s.replace("d ago", ""))
            if days > 7:
                warn("SEO run is older than 7 days -- consider re-running /seo-analyze")
    else:
        warn("runs -- no SEO runs found")
except Exception as e:
    bad(f"runs -- {e}")

kw_total  = count_total("domain_ranked_keywords")
iss_total = count_total("issues")
print(f"  domain_ranked_keywords  {kw_total} rows")
print(f"  issues                  {iss_total} rows")

# ── SUMMARY ───────────────────────────────────────────
print(f"\n{B}{'='*60}{X}")
print(f"{B}  SUMMARY{X}")
print(f"{B}{'='*60}{X}")

items = [
    ("IG posts scraped today",        ig_today,    "gt0"),
    ("IG post analysis coverage",     ipa_null,    "zero"),
    ("IG comment analysis coverage",  ica_null,    "zero"),
    ("TikTok videos scraped today",   tt_today,    "gt0"),
    ("TikTok enriched",               tt_enr_null, "zero"),
    ("X posts scraped today",         xp_today,    "gt0"),
    ("X enriched",                    xp_enr_null, "zero"),
    ("X replies enriched",            xr_sent_null,"zero"),
    ("Reddit scraped today",          rd_today,    "gt0"),
    ("Reddit topics enriched",        rd_top_null, "zero"),
    ("Reddit sentiment enriched",     rd_sent_null,"warn_if_nonzero"),
    ("News scraped today",            na_today,    "gt0"),
    ("News ai_summary coverage",      na_sum_null, "warn_if_nonzero"),
]

passed = failed = warned = 0
for label, val, rule in items:
    if isinstance(val, str):
        warn(f"{label}: {val}"); warned += 1
    elif rule == "gt0":
        if val > 0: ok(f"{label} ({val})"); passed += 1
        else: bad(f"{label} -- 0 rows"); failed += 1
    elif rule == "zero":
        if val == 0: ok(f"{label}"); passed += 1
        else: bad(f"{label} -- {val} unenriched"); failed += 1
    elif rule == "warn_if_nonzero":
        if val == 0: ok(f"{label}"); passed += 1
        else: warn(f"{label} -- {val} unenriched"); warned += 1

print(f"\n  {G}{passed} passed{X}   {Y}{warned} warnings{X}   {R}{failed} failed{X}\n")
