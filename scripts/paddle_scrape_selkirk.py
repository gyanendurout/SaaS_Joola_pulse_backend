"""Stage 3 — backfill Selkirk paddle reviews from Okendo (plan §2.2, Phase 3).

Reads the already-staged Selkirk Shopify catalog, walks every product's Okendo
review pages, dedupes on `reviewId` run-wide, and stages the result to disk.
The DB write is best-effort: migration 011 may not be applied yet, and staging
to JSONL means a 15-20 minute scrape is never lost to that.

Usage:
    ./.venv/Scripts/python.exe scripts/paddle_scrape_selkirk.py [--limit N] [--sanity]

    --limit N   scrape only the first N products (smoke test)
    --sanity    scrape only Selkirk OMNI and assert the 341-review walk from
                the plan still reproduces
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from typing import Any

from app.db import service_client
from app.services import okendo
from app.services import paddle_store as ps

CATALOG_STAGE = "products_shopify"
SELKIRK_SOURCE = "selkirk_shopify"
REVIEWS_STAGE = "reviews_okendo"
STATS_STAGE = "product_stats_okendo"

# Selkirk OMNI — the product the plan's 100+100+100+41 walk was verified on.
SANITY_PRODUCT_ID = "8000169181286"
SANITY_EXPECTED_MIN = 341


def selkirk_products() -> list[dict[str, Any]]:
    """Staged Selkirk paddles. Fails loudly rather than scraping nothing."""
    rows = [
        r for r in ps.read_staged(CATALOG_STAGE)
        if r.get("source") == SELKIRK_SOURCE
    ]
    if not rows:
        raise SystemExit(
            f"No '{SELKIRK_SOURCE}' rows in staged '{CATALOG_STAGE}'. "
            "Run scripts/paddle_catalog_stage.py first."
        )
    return rows


def _persist(rows: list[dict[str, Any]]) -> None:
    """Upsert to Supabase when migration 011 is applied; otherwise say so."""
    db = service_client()
    if not ps.tables_ready(db):
        print("DB: migration 011 not applied yet -> staging-only, nothing upserted")
        return
    written = ps.upsert_reviews(db, rows)
    print(f"DB: upserted {written} reviews")


async def _sanity_check() -> int:
    """Reproduce the plan's verified OMNI walk before trusting a full run."""
    products = [p for p in selkirk_products() if p["source_product_id"] == SANITY_PRODUCT_ID]
    if not products:
        raise SystemExit(f"Sanity product {SANITY_PRODUCT_ID} not in staged catalog")

    result = await okendo.scrape_products(products)
    summary = result.summary()
    print(json.dumps(summary))

    unique = summary["reviews_unique"]
    if unique < SANITY_EXPECTED_MIN:
        print(
            f"SANITY FAIL: expected >= {SANITY_EXPECTED_MIN} unique reviews on OMNI, "
            f"got {unique}",
            file=sys.stderr,
        )
        return 1
    if summary["errors"]:
        print("SANITY FAIL: errors during the OMNI walk", file=sys.stderr)
        return 1
    print(f"SANITY OK: {unique} unique reviews, 0 errors (plan baseline {SANITY_EXPECTED_MIN})")
    return 0


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scrape Selkirk reviews from Okendo")
    parser.add_argument("--limit", type=int, default=None, help="scrape only the first N products")
    parser.add_argument("--sanity", action="store_true", help="verify the OMNI 341-review walk")
    args = parser.parse_args(argv)

    if args.sanity:
        return await _sanity_check()

    products = selkirk_products()
    if args.limit:
        products = products[: args.limit]
    print(f"Scraping {len(products)} Selkirk paddles from Okendo...")

    result = await okendo.scrape_products(products)

    review_rows = [r.to_row() for r in result.reviews]
    ps.write_staged(REVIEWS_STAGE, review_rows)
    ps.write_staged(STATS_STAGE, [asdict(s) for s in result.stats])

    _persist(review_rows)
    print(json.dumps(result.summary()))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
