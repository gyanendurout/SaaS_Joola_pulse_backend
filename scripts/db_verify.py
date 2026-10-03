"""
db_verify.py - Full 4-phase pipeline cross-verification
Checks all 24 tables for scrape freshness (P1), enrichment coverage (P2),
fact population (P3), and sales intel (P4).
Usage: python scripts/db_verify.py
"""
import sys
import io
import os
from datetime import datetime, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

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

TODAY      = datetime.now(timezone.utc).date().isoformat()
TODAY_START = f"{TODAY}T00:00:00+00:00"

G = "\033[92m"
Y = "\033[93m"
R = "\033[91m"
B = "\033[1m"
RST = "\033[0m"

summary_rows = []


def _count(table, col=None, op=None, val=None):
    try:
        q = sb.table(table).select("id", count="exact")
        if op == "gte":
            q = q.gte(col, val)
        elif op == "null":
            q = q.is_(col, "null")
        elif op == "notnull":
            q = q.not_.is_(col, "null")
        return q.limit(0).execute().count or 0
    except Exception as e:
        return f"ERR({str(e)[:60]})"


def _latest(table, col):
    try:
        r = sb.table(table).select(col).order(col, desc=True).limit(1).execute()
        return (r.data[0][col] or "")[:19] if r.data else "none"
    except Exception as e:
        return f"ERR({str(e)[:40]})"


def _ok(msg):   print(f"  {G}[OK]  {RST}{msg}")
def _warn(msg): print(f"  {Y}[WARN]{RST} {msg}")
def _bad(msg):  print(f"  {R}[FAIL]{RST} {msg}")
def _miss(msg): print(f"  {Y}[MISS]{RST} {msg}")


def check_freshness(label, table, date_col, note=None):
    total   = _count(table)
    today_n = _count(table, date_col, "gte", TODAY_START)
    suffix  = f"  ({note})" if note else ""
    is_ok = isinstance(today_n, int) and today_n > 0
    if isinstance(today_n, str):
        line = f"{R}{today_n}{RST}"
        summary_rows.append((label, "WARN"))
    elif today_n > 0:
        line = f"{G}{today_n} new today{RST} | total: {total}{suffix}"
        summary_rows.append((label, "OK"))
    else:
        latest = _latest(table, date_col)
        line = f"{R}0 new today{RST} | total: {total} | latest: {latest}{suffix}"
        summary_rows.append((label, "FAIL"))
    print(f"  {B}{label:<30}{RST}  {line}")


def check_count_only(label, table, note=None):
    total  = _count(table)
    latest = _latest(table, "created_at") if not isinstance(total, str) else "?"
    suffix = f"  ({note})" if note else ""
    if isinstance(total, str):
        print(f"  {B}{label:<30}{RST}  {R}{total}{RST}")
        summary_rows.append((label, "WARN"))
    else:
        color = G if total > 0 else Y
        print(f"  {B}{label:<30}{RST}  {color}{total} rows{RST} | latest: {latest}{suffix}")
        summary_rows.append((label, "OK" if total > 0 else "MISS"))


def check_enrichment(label, table, enr_col="enriched_at"):
    total    = _count(table)
    enriched = _count(table, enr_col, "notnull")
    null_n   = _count(table, enr_col, "null")
    if isinstance(total, str) or isinstance(null_n, str):
        print(f"  {B}{label:<30}{RST}  {R}ERR{RST}")
        summary_rows.append((label + " enrichment", "WARN"))
        return
    if total == 0:
        print(f"  {B}{label:<30}{RST}  {Y}no rows{RST}")
        summary_rows.append((label + " enrichment", "MISS"))
        return
    pct_done = round(100 * enriched / total)
    if null_n == 0:
        print(f"  {B}{label:<30}{RST}  {G}fully enriched ({total}){RST}")
        summary_rows.append((label + " enrichment", "OK"))
    elif pct_done >= 80:
        print(f"  {B}{label:<30}{RST}  {Y}{null_n}/{total} unenriched ({100-pct_done}%){RST}")
        summary_rows.append((label + " enrichment", "WARN"))
    else:
        print(f"  {B}{label:<30}{RST}  {R}{null_n}/{total} unenriched ({100-pct_done}%){RST}")
        summary_rows.append((label + " enrichment", "FAIL"))


# ============================================================
print(f"\n{B}{'='*64}{RST}")
print(f"{B}  JOOLA Pulse -- Full Pipeline Verification (4 phases){RST}")
print(f"  Date: {TODAY}")
print(f"{B}{'='*64}{RST}\n")

# ── P1: SCRAPING ─────────────────────────────────────────────
print(f"{B}[ P1 -- SCRAPING ] (15 tables){RST}")

check_freshness("ig_posts",          "joola_ig_posts",     "scraped_at")
check_freshness("ig_comments",       "joola_ig_comments",  "scraped_at")
check_freshness("yt_videos",         "yt_videos",          "published_at", "upload date not scrape date")
check_freshness("yt_comments",       "yt_comments",        "scraped_at")
check_freshness("reddit_mentions",   "reddit_mentions",    "scraped_at")
check_freshness("reddit_comments",   "reddit_comments",    "created_at")
check_freshness("x_posts",           "x_posts",            "created_at")
check_freshness("tiktok_videos",     "tiktok_videos",      "created_at")
check_freshness("tiktok_comments",   "tiktok_comments",    "scraped_at")
check_freshness("influencer_posts",  "influencer_posts",   "scraped_at")
check_freshness("influencer_x_posts","influencer_x_posts", "created_at")
check_freshness("marketing_ads",     "marketing_ads",      "captured_at")
check_freshness("promotions",        "promotions",         "detected_at")
check_freshness("inventory_events",  "inventory_events",   "event_time")
check_count_only("products_catalog", "products_catalog")

# ── P2: ENRICHMENT ────────────────────────────────────────────
print(f"\n{B}[ P2 -- ENRICHMENT ] (8 tables){RST}")

check_enrichment("ig_post_analysis",   "joola_ig_post_analysis", "content_theme")
check_enrichment("tiktok_videos",      "tiktok_videos",     "enriched_at")
check_enrichment("tiktok_comments",    "tiktok_comments",   "enriched_at")
check_enrichment("yt_comments",        "yt_comments",       "enriched_at")
check_enrichment("reddit_comments",    "reddit_comments",   "enriched_at")
check_enrichment("x_posts",            "x_posts",           "enriched_at")
check_enrichment("influencer_x_posts", "influencer_x_posts","enriched_at")
check_enrichment("yt_video_analysis",  "yt_video_analysis", "enriched_at")

# ── P3: FACTS ────────────────────────────────────────────────
print(f"\n{B}[ P3 -- FACTS ] (5 tables){RST}")

check_count_only("mention_facts",           "mention_facts")
check_count_only("topic_lifecycle",         "topic_lifecycle")
check_count_only("product_attention_daily", "product_attention_daily")
check_count_only("competitor_switch_events","competitor_switch_events")
check_count_only("product_mentions",        "product_mentions")

# ── P4: SALES INTEL ──────────────────────────────────────────
print(f"\n{B}[ P4 -- SALES INTEL ] (3 tables){RST}")

check_count_only("sales_estimates",       "sales_estimates")
check_count_only("sales_facts_daily",     "sales_facts_daily")
check_count_only("promotion_sales_impact","promotion_sales_impact")

# ── SUMMARY ──────────────────────────────────────────────────
ok_n   = sum(1 for _, s in summary_rows if s == "OK")
fail_n = sum(1 for _, s in summary_rows if s == "FAIL")
warn_n = sum(1 for _, s in summary_rows if s in ("WARN", "MISS"))

print(f"\n{B}{'='*64}{RST}")
print(f"{B}  SUMMARY  --  {ok_n} passed  |  {warn_n} warnings  |  {fail_n} failed{RST}")
print(f"{B}{'='*64}{RST}")
for label, status in summary_rows:
    if status == "FAIL":
        _bad(label)
    elif status in ("WARN", "MISS"):
        _warn(label)
    else:
        _ok(label)
print()
