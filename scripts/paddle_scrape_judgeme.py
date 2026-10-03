"""Stage 4 — Judge.me reviews for Paddletek + CRBN (+ Six Zero aggregates).

Reads the already-staged Shopify catalog, scrapes every Paddletek/CRBN/Six Zero
paddle through the public `reviews_for_widget` endpoint, and stages the result.

Six Zero runs the Judge.me v3 React widget, which publishes only aggregates —
`text_unavailable` is the accepted outcome there (plan decision D13), and its
ratings still land in `product_stats_judgeme`.

Exits non-zero if any rating failed to parse, so a Judge.me markup change
breaks the pipeline loudly instead of persisting NULL ratings (plan §8).
"""
from __future__ import annotations

import asyncio
import collections
import json
import sys

from app.db import service_client
from app.services import paddle_store as ps
from app.services.judgeme import SOURCE_TO_SLUG, ProductResult, scrape_products

CATALOG_SOURCES = tuple(SOURCE_TO_SLUG)


def _staged_products() -> list[dict]:
    rows = [r for r in ps.read_staged("products_shopify")
            if r.get("source") in CATALOG_SOURCES]
    if not rows:
        raise SystemExit(
            "no staged Judge.me products — run scripts/paddle_catalog_stage.py first"
        )
    return rows


def _dedupe(results: list[ProductResult]) -> list[dict]:
    """Flatten to rows, dropping ids already seen on another product page.

    Judge.me lists a review under every variant of a paddle family, so the same
    `data-review-id` can arrive twice across products.
    """
    by_id: dict[str, dict] = {}
    for res in results:
        for rec in res.reviews:
            row = rec.finalise().to_row()
            by_id.setdefault(row["external_review_id"], row)
    return list(by_id.values())


def _product_stats(results: list[ProductResult]) -> list[dict]:
    return [
        {
            "source": res.source,
            "source_product_id": res.source_product_id,
            "review_count": res.review_count,
            "avg_rating": res.avg_rating,
        }
        for res in results
    ]


async def main() -> int:
    products = _staged_products()
    results = await scrape_products(products)

    review_rows = _dedupe(results)
    ps.write_staged("reviews_judgeme", review_rows)
    ps.write_staged("product_stats_judgeme", _product_stats(results))

    per_brand = collections.Counter(r["brand"] for r in review_rows)
    statuses = collections.Counter(res.status for res in results)
    failures = sum(res.rating_parse_failures for res in results)
    errors = sum(1 for res in results if res.status == "error")

    db = service_client()
    if ps.tables_ready(db):
        n = ps.upsert_reviews(db, review_rows)
        print(f"DB: upserted {n} reviews")
    else:
        print("DB: migration 011 not applied yet -> staged to disk only")

    for res in results:
        if res.error:
            print(f"ERROR {res.brand} {res.source_product_id}: {res.error[:180]}")

    sixzero = [res for res in results if res.source == "sixzero_shopify"]
    sixzero_status = (
        "text_unavailable"
        if sixzero and all(r.status in ("text_unavailable", "empty") for r in sixzero)
        else "mixed" if sixzero else "not_scraped"
    )
    print(json.dumps({
        "products_scraped": len(results),
        "reviews_unique": len(review_rows),
        "per_brand": dict(per_brand),
        "rating_parse_failures": failures,
        "sixzero_status": sixzero_status,
        "errors": errors,
        "statuses": dict(statuses),
        "sixzero_rated_products": sum(1 for r in sixzero if r.review_count),
        "sixzero_reviews_reported": sum(r.review_count for r in sixzero),
    }))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
