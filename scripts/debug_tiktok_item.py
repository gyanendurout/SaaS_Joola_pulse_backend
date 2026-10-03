"""One-shot debug: fetch a known Apify run dataset and print raw item fields."""
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

token = os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN", "")
# run Ric61UyoYRRSOVW91 = video 4 (fetched 2 raw comments)
run_id = "Ric61UyoYRRSOVW91"
url = f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items?token={token}&clean=true"
r = requests.get(url, timeout=30)
items = r.json()
print(f"Count: {len(items)}")
for i, item in enumerate(items[:3]):
    print(f"\n--- item {i} keys: {list(item.keys())}")
    print(json.dumps(item, indent=2, default=str)[:800])
