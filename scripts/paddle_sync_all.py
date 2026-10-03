"""ONE COMMAND to refresh all paddle review intelligence.

    .venv/Scripts/python.exe scripts/paddle_sync_all.py

That crawls every source, loads Supabase, repairs its own referential gaps and
AI-enriches whatever is new. Safe to run weekly, monthly or ad hoc — every step is
idempotent, so a re-run converges instead of duplicating.

What it does, in order:
  1  CATALOG       5 brand storefronts (Shopify products.json)
  2  BAZAARVOICE   joola.com reviews
  3  OKENDO        selkirk.com reviews
  4  JUDGEME       paddletek.com + crbnpickleball.com (Six Zero = ratings only)
  5  PBC CATALOG   pickleballcentral.com ids (public Searchspring feed)
  6  YOTPO         pickleballcentral.com reviews — the cross-brand retailer
  7  DICKS         dickssportinggoods.com catalog (sitemap harvest, no network)
  8  DICKS REVIEWS gated on APIFY_ENABLED + DSG_BV_* (plan O3)
  9  AMAZON        gated on Brand Registry or APIFY_ENABLED (plan O1)
 10  NORMALISE     syndication flags · product aggregates · orphan products · FK links
 11  ENRICH        sentiment, topics, crisis, complaint category, competitors

Self-healing properties:
  * every stage has a timeout and one retry; a dead source cannot hang the run
  * a failing source is recorded and skipped — the other eight still complete
  * scrapes stage to disk BEFORE the DB write, so nothing is lost on a DB blip
  * enrichment resumes from `sentiment_label IS NULL`, so a kill costs one batch
  * orphaned reviews get their product row created automatically
  * exit code: 0 all good · 1 partial (some source failed) · 2 fatal

Options:
  --sources a,b     run only these stages (names above, lowercased)
  --skip-enrich     crawl and load but do not spend LLM budget
  --quiet           only print the final JSON summary
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from typing import Any

from app.agents.paddle_reviews import STAGES, sync_all_sources
from app.db import service_client
from app.services import paddle_store as ps

MIGRATION_HINT = """
FATAL: the paddle_* tables do not exist — migration 011 has not been applied.

  1. Supabase dashboard -> SQL editor
  2. paste backend/supabase/migrations/011_paddle_reviews.sql -> Run
  3. re-run this script

Crawled data is never lost meanwhile: it stages to
backend/storage/paddle_staging/ and `scripts/paddle_load_staged.py` replays it.
""".strip()


def _before(db: Any) -> dict[str, int]:
    def count(table: str) -> int:
        try:
            return db.table(table).select("id", count="exact").limit(1).execute().count or 0
        except Exception:
            return 0
    return {"products": count("paddle_products"), "reviews": count("paddle_reviews")}


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Crawl every paddle review source and refresh Supabase.",
    )
    ap.add_argument("--sources", default=None, help=f"subset of: {','.join(STAGES)}")
    ap.add_argument("--skip-enrich", action="store_true", help="skip the AI enrichment stage")
    ap.add_argument("--quiet", action="store_true", help="print only the final summary")
    args = ap.parse_args()

    started = time.monotonic()
    db = service_client()

    if not ps.tables_ready(db):
        print(MIGRATION_HINT, file=sys.stderr)
        return 2

    wanted = [s.strip() for s in (args.sources or "").split(",") if s.strip()] or None
    unknown = [s for s in (wanted or []) if s not in STAGES]
    if unknown:
        print(f"FATAL: unknown source(s) {unknown}; valid: {list(STAGES)}", file=sys.stderr)
        return 2

    before = _before(db)
    if not args.quiet:
        print(f"start: {before['products']} products, {before['reviews']} reviews in DB")
        print(f"stages: {wanted or list(STAGES)}", flush=True)

    run_id = str(uuid.uuid4())
    db.table("paddle_review_runs").insert({
        "id": run_id,
        "status": "pending",
        "run_type": "scheduled",
        "stages_total": len(wanted or STAGES),
    }).execute()

    try:
        summary = await sync_all_sources(run_id, wanted, not args.skip_enrich)
    except Exception as exc:                      # last-resort net: always close the run row
        db.table("paddle_review_runs").update({
            "status": "error", "error_message": str(exc)[:500], "finished_at": ps.utcnow_iso(),
        }).eq("id", run_id).execute()
        print(json.dumps({"run_id": run_id, "status": "error", "error": str(exc)}, indent=2))
        return 2

    after = _before(db)
    summary["db_before"] = before
    summary["db_after"] = after
    summary["products_added"] = after["products"] - before["products"]
    summary["reviews_added"] = after["reviews"] - before["reviews"]
    summary["minutes"] = round((time.monotonic() - started) / 60, 1)

    print(json.dumps(summary, indent=2, default=str))

    if summary["failed_sources"]:
        print(
            f"PARTIAL: {len(summary['failed_sources'])} source(s) failed — "
            f"{summary['failed_sources']}. Re-run to retry; loaded data is unaffected.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
