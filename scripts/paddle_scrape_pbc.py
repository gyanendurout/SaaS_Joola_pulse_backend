"""Stage 6b — Pickleball Central: BigCommerce catalog + Yotpo cross-brand reviews.

Run:  ./.venv/Scripts/python.exe scripts/paddle_scrape_pbc.py [--smoke] [--brands joola,crbn]

`--smoke` skips the sweep and only exercises BigCommerce product 8152, whose
known-good answer is 11 reviews at 4.82 average (PADDLE_REVIEWS_PLAN.md §2.5).

Staging is the expected path today: migration 011 is not applied, so the script
writes JSONL under `storage/paddle_staging/` and says so. It never creates
tables and never talks to Postgres directly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from typing import Any

import httpx

from app.services import bigcommerce_catalog as bc
from app.services import paddle_store as ps
from app.services import yotpo

SMOKE_PRODUCT_ID = "8152"
SMOKE_EXPECT_REVIEWS = 11
SMOKE_EXPECT_AVG = 4.82


# ---------------------------------------------------------------------------- #
# Catalog                                                                        #
# ---------------------------------------------------------------------------- #

async def build_catalog(brand_ids: dict[str, str], slugs: list[str] | None) -> tuple[
    list[dict[str, Any]], dict[str, int], list[str]
]:
    """Discover + enumerate the PBC paddle catalog. Returns (rows, per_brand, notes)."""
    notes: list[str] = []

    # Discovery runs first because it is also the access check: if a genuine
    # browser is refused, we record it and carry on with reduced coverage
    # rather than escalating (D14).
    urls, err = await bc.discover_brand_category_urls()
    if err:
        notes.append(f"storefront discovery failed ({err}); brand URLs unverified")
    else:
        missing = [b.slug for b in bc.TRACKED_BRANDS if b.slug not in urls]
        notes.append(f"discovered {len(urls)}/{len(bc.TRACKED_BRANDS)} brand category URLs")
        if missing:
            notes.append(f"no by-brand URL found for: {', '.join(missing)}")

    catalogs = await bc.fetch_all_products(slugs=slugs, paddles_only=True)

    rows: list[dict[str, Any]] = []
    per_brand: dict[str, int] = {}
    seen_ids: set[str] = set()
    for brand in bc.TRACKED_BRANDS:
        items = catalogs.get(brand.slug, [])
        kept = 0
        for item in items:
            record = bc.to_product_record(item, brand_ids, fallback=brand)
            if record is None or record.source_product_id in seen_ids:
                continue
            seen_ids.add(record.source_product_id)
            row = record.to_row()
            row["product_url"] = row.get("product_url") or urls.get(brand.slug)
            rows.append(row)
            kept += 1
        per_brand[brand.slug] = kept

    if not rows and not err:
        notes.append("Searchspring returned nothing; DOM fallback available "
                     "via bigcommerce_catalog.enumerate_ids_via_browser")
    return rows, per_brand, notes


# ---------------------------------------------------------------------------- #
# Reviews                                                                        #
# ---------------------------------------------------------------------------- #

def dedupe_records(records: list[Any]) -> list[Any]:
    """Dedupe on the Yotpo review id, then finalise each survivor."""
    by_id: dict[str, Any] = {}
    for rec in records:
        rid = str(rec.external_review_id or "")
        if rid:
            by_id.setdefault(rid, rec)
    return [rec.finalise() for rec in by_id.values()]


def count_shared_hashes(rows: list[dict[str, Any]]) -> int:
    """How many PBC reviews share a `content_hash` with another PBC review.

    `(source, external_review_id)` cannot see a syndicated duplicate (§8), so
    this is the intra-source half of that check; the cross-source half runs once
    the brand-site feeds land in the same table.
    """
    counts = Counter(r.get("content_hash") for r in rows if r.get("content_hash"))
    return sum(n for n in counts.values() if n > 1)


async def run_smoke() -> int:
    """Verify the endpoint against the plan's known-good product before sweeping."""
    async with httpx.AsyncClient(
        timeout=yotpo.REQUEST_TIMEOUT, headers={"User-Agent": yotpo.UA}
    ) as client:
        raws, bottomline = await yotpo.fetch_product_reviews(client, SMOKE_PRODUCT_ID)

    avg = yotpo.avg_rating(bottomline)
    ok = len(raws) == SMOKE_EXPECT_REVIEWS and avg == SMOKE_EXPECT_AVG
    print(json.dumps({
        "smoke_product": SMOKE_PRODUCT_ID,
        "reviews": len(raws),
        "avg_rating": avg,
        "expected": {"reviews": SMOKE_EXPECT_REVIEWS, "avg_rating": SMOKE_EXPECT_AVG},
        "pass": ok,
    }, indent=2))
    if raws:
        sample = yotpo.to_review_record(raws[0], bc_product_id=SMOKE_PRODUCT_ID).finalise()
        print(json.dumps({
            "sample": {
                "external_review_id": sample.external_review_id,
                "rating": sample.rating,
                "title": sample.title,
                "body": (sample.body or "")[:160],
                "reviewer_name": sample.reviewer_name,
                "is_verified": sample.is_verified,
                "source_sentiment": sample.source_sentiment,
                "context_values": sample.context_values,
                "content_hash": (sample.content_hash or "")[:16],
            }
        }, indent=2))
    return 0 if ok else 1


# ---------------------------------------------------------------------------- #
# Main                                                                           #
# ---------------------------------------------------------------------------- #

async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="endpoint check only")
    parser.add_argument("--brands", default="", help="comma-separated brand slugs")
    args = parser.parse_args()

    if args.smoke:
        return await run_smoke()

    slugs = [s.strip() for s in args.brands.split(",") if s.strip()] or None

    db: Any = None
    brand_ids: dict[str, str] = {}
    try:
        from app.db import service_client

        db = service_client()
        brand_ids = ps.brand_id_map(db)
    except Exception as e:                         # staging still works without it
        print(f"NOTE: Supabase client unavailable ({str(e)[:120]}) -> brand_id left null")

    product_rows, per_brand, notes = await build_catalog(brand_ids, slugs)
    ps.write_staged("products_pbc", product_rows)

    records, stats, errors = await yotpo.fetch_reviews_for_products(product_rows)
    finalised = dedupe_records(records)
    review_rows = [r.to_row() for r in finalised]

    ps.write_staged("reviews_yotpo", review_rows)
    ps.write_staged("product_stats_yotpo", stats)

    custom_keys = sorted({
        k for r in review_rows for k in (r.get("context_values") or {})
    })
    summary = {
        "products_found": len(product_rows),
        "per_brand": per_brand,
        "products_with_reviews": sum(1 for s in stats if s["review_count"] > 0),
        "reviews_unique": len(review_rows),
        "dupe_content_hashes": count_shared_hashes(review_rows),
        "custom_field_keys_seen": custom_keys,
        "errors": len(errors),
    }

    for note in notes:
        print(f"NOTE: {note}")
    if errors:
        print("ERRORS: " + json.dumps(errors[:5]))

    if db is not None and ps.tables_ready(db):
        print(f"DB: upserted {ps.upsert_products(db, product_rows)} products")
        print(f"DB: upserted {ps.upsert_reviews(db, review_rows)} reviews")
    else:
        print("DB: migration 011 not applied yet -> staged to disk only "
              "(storage/paddle_staging/)")

    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
