"""Debug: check tiktok_comments table state and existing comment IDs."""
import os, sys, json
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

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
}

# 1. Total row count in tiktok_comments
r = requests.get(
    f"{SUPABASE_URL}/rest/v1/tiktok_comments?select=id&limit=5",
    headers={**headers, "Prefer": "count=exact"},
    timeout=15,
)
print(f"tiktok_comments count status: {r.status_code}")
print(f"Content-Range: {r.headers.get('Content-Range', 'n/a')}")
print(f"First 5 rows: {r.text[:300]}")

# 2. Check video 4's UUID (tiktok_video_id=7637856956245495053)
r2 = requests.get(
    f"{SUPABASE_URL}/rest/v1/tiktok_videos?select=id,tiktok_video_id&tiktok_video_id=eq.7637856956245495053",
    headers=headers,
    timeout=15,
)
print(f"\nvideo lookup status: {r2.status_code}")
print(f"video row: {r2.text}")

# 3. If video found, check existing comments for that video_id
rows2 = r2.json()
if rows2:
    vid_uuid = rows2[0]["id"]
    print(f"\nvideo_db_id (uuid): {vid_uuid}")
    r3 = requests.get(
        f"{SUPABASE_URL}/rest/v1/tiktok_comments?select=tiktok_comment_id&video_id=eq.{vid_uuid}",
        headers=headers,
        timeout=15,
    )
    print(f"existing comments query status: {r3.status_code}")
    print(f"existing: {r3.text[:500]}")
