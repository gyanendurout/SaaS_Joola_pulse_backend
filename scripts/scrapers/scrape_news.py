"""Standalone news scraper — runs without the FastAPI server.

Usage (from the backend directory, with venv activated):
    python scripts/scrapers/scrape_news.py

To run silently in the background and keep running even if you close the
terminal, use scripts/run_scrape_bg.ps1 instead.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Resolve the backend root (2 levels up: scrapers/ → scripts/ → backend/)
BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(BACKEND_ROOT)
sys.path.insert(0, str(BACKEND_ROOT))

from app.db import service_client
from app.agents.news_scraper import scrape_all_sites, SITES


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


async def main() -> None:
    run_id = str(uuid.uuid4())
    print(f"[scrape_news] run_id  : {run_id}")
    print(f"[scrape_news] sites   : {len(SITES)}")
    print(f"[scrape_news] started : {_utcnow()}")
    print()

    db = service_client()
    db.table("news_scrape_runs").insert({
        "id": run_id,
        "status": "pending",
        "run_type": "cli",
        "lookback_days": 180,
        "sites_total": len(SITES),
        "sites_scraped": 0,
        "articles_found": 0,
        "articles_new": 0,
        "articles_with_mentions": 0,
        "created_at": _utcnow(),
    }).execute()

    await scrape_all_sites(run_id)

    result = db.table("news_scrape_runs").select("*").eq("id", run_id).single().execute()
    r = result.data
    print()
    print("=" * 48)
    print(f"[scrape_news] DONE — {_utcnow()}")
    print(f"  Sites scraped  : {r['sites_scraped']} / {r['sites_total']}")
    print(f"  Articles found : {r['articles_found']}")
    print(f"  New stored     : {r['articles_new']}  (JOOLA-related only)")
    print(f"  JOOLA-related  : {r.get('joola_related_articles', 0)}")
    print(f"  Successful     : {r['successful_sources']}")
    print(f"  Failed sources : {r['failed_sources']}")
    print("=" * 48)


if __name__ == "__main__":
    asyncio.run(main())
