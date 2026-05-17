#!/usr/bin/env python
"""Standalone news scraper — runs without the FastAPI server.

Usage (from the backend directory, with venv activated):
    python scrape_now.py

To run silently in the background and keep running even if you close the terminal,
use run_scrape_bg.ps1 instead.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Ensure imports and .env resolve relative to this file's directory
os.chdir(Path(__file__).parent)
sys.path.insert(0, str(Path(__file__).parent))

from app.db import service_client
from app.agents.news_scraper import scrape_all_sites, SITES


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


async def main() -> None:
    run_id = str(uuid.uuid4())
    print(f"[scrape_now] run_id  : {run_id}")
    print(f"[scrape_now] sites   : {len(SITES)}")
    print(f"[scrape_now] started : {_utcnow()}")
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

    # Fetch final stats from DB
    result = db.table("news_scrape_runs").select("*").eq("id", run_id).single().execute()
    r = result.data
    print()
    print("=" * 48)
    print(f"[scrape_now] DONE — {_utcnow()}")
    print(f"  Sites scraped  : {r['sites_scraped']} / {r['sites_total']}")
    print(f"  Articles found : {r['articles_found']}")
    print(f"  New stored     : {r['articles_new']}  (JOOLA-related only)")
    print(f"  JOOLA-related  : {r.get('joola_related_articles', 0)}")
    print(f"  Successful     : {r['successful_sources']}")
    print(f"  Failed sources : {r['failed_sources']}")
    print("=" * 48)


if __name__ == "__main__":
    asyncio.run(main())
