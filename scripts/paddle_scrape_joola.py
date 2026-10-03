"""Stage 2 — scrape every staged JOOLA paddle's Bazaarvoice reviews.

Reads the products staged by ``scripts/paddle_catalog_stage.py``, walks the
Bazaarvoice review API for each one, dedupes on review id across the whole run
(the API is family-expanded, so sibling variants return overlapping reviews),
and writes two staging files:

  * ``reviews_bazaarvoice``       — one ReviewRecord row per unique review
  * ``product_stats_bazaarvoice`` — per-product count / average / distribution,
                                    so paddle_products can be updated later

Upserts to Supabase only when migration 011 has been applied; otherwise the
staging files are the deliverable and nothing is lost.
"""
from __future__ import annotations

import asyncio
import json
import sys

from app.services import paddle_store as ps
from app.services.bazaarvoice import scrape_catalog

CATALOG_STAGE = "products_shopify"
CATALOG_SOURCE = "joola_shopify"
REVIEWS_STAGE = "reviews_bazaarvoice"
STATS_STAGE = "product_stats_bazaarvoice"


def joola_products() -> list[dict]:
    """Staged JOOLA Shopify products — the only catalog Bazaarvoice serves."""
    return [
        row for row in ps.read_staged(CATALOG_STAGE)
        if row.get("source") == CATALOG_SOURCE
    ]


def _db_or_none():
    """Supabase client, or None when it is not configured in this environment.

    A missing client must not fail the scrape: the staging files are written
    first and are the source of truth until migration 011 lands.
    """
    try:
        from app.db import service_client
        return service_client()
    except Exception as e:                                   # pragma: no cover
        print(f"DB: client unavailable ({str(e)[:120]}) -> staging only")
        return None


async def main() -> int:
    products = joola_products()
    if not products:
        print(f"No staged {CATALOG_SOURCE} products found in '{CATALOG_STAGE}'. "
              "Run scripts/paddle_catalog_stage.py first.")
        return 1

    result = await scrape_catalog(products)

    review_rows = [r.to_row() for r in result.records]
    ps.write_staged(REVIEWS_STAGE, review_rows)
    ps.write_staged(STATS_STAGE, result.product_stats)

    db = _db_or_none()
    if db is not None and ps.tables_ready(db):
        written = ps.upsert_reviews(db, review_rows)
        print(f"DB: upserted {written} reviews into paddle_reviews")
    else:
        print("DB: migration 011 not applied yet -> staged to disk only")

    for err in result.errors[:10]:
        print(f"ERROR {err['source_product_id']}: {err['error']}")

    print(json.dumps({
        "products_scraped": result.products_scraped,
        "reviews_unique": len(result.records),
        "reviews_with_null_reviewer": sum(
            1 for r in result.records if not r.reviewer_name
        ),
        "families": len(result.families),
        "errors": len(result.errors),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
