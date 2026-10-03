"""Quick diagnostic: check enriched_at / sentiment column state for TikTok, X, Reddit."""
import os
import sys
import requests
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent

def load_env(path):
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env(BACKEND_ROOT / ".env")

URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "")
KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}

def q(table, select, qs=""):
    url = f"{URL}/rest/v1/{table}?select={select}" + (f"&{qs}" if qs else "")
    r = requests.get(url, headers=H, timeout=15)
    return r.status_code, r.json()

# TikTok
status, rows = q("tiktok_videos", "tiktok_video_id,enriched_at,sentiment_label", "limit=3")
print(f"\nTikTok sample (status {status}):", rows[:3] if isinstance(rows, list) else rows)

status2, rows2 = q("tiktok_videos", "tiktok_video_id", "enriched_at=is.null")
print(f"TikTok enriched_at=null: {len(rows2) if isinstance(rows2, list) else rows2} rows")

# X/Twitter
status3, rows3 = q("x_posts", "tweet_id,enriched_at,sentiment_label", "limit=3")
print(f"\nX sample (status {status3}):", rows3[:3] if isinstance(rows3, list) else rows3)

status4, rows4 = q("x_posts", "tweet_id", "enriched_at=is.null")
print(f"X enriched_at=null: {len(rows4) if isinstance(rows4, list) else rows4} rows")

# Reddit
status5, rows5 = q("reddit_mentions", "reddit_post_id,sentiment,topics", "limit=3")
print(f"\nReddit sample (status {status5}):", rows5[:3] if isinstance(rows5, list) else rows5)

status6, rows6 = q("reddit_mentions", "reddit_post_id", "sentiment=is.null")
print(f"Reddit sentiment=null: {len(rows6) if isinstance(rows6, list) else rows6} rows")
