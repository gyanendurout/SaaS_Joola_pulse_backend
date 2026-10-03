"""Resolve reviews whose product row is missing, then re-link.

Why this is needed: Bazaarvoice `productid` filtering is family-expanded, so a
query for one paddle returns reviews whose own `ProductId` is a sibling variant.
Some of those siblings are colourway products (e.g. "Hyperion CFS 16mm - Vice
Blue") that live OUTSIDE `/collections/pickleball-paddles`, so the catalog stage
never saw them and the review has nothing to link to.

This looks each orphan id up in the brand's *full* `products.json`, creates the
missing `paddle_products` row, and re-runs linking. Ids that no longer resolve
are delisted products; their reviews stay unlinked but keep `brand`,
`canonical_name` and `family_id`, so they remain usable in analytics.

Usage (from `backend/`):
    .venv/Scripts/python.exe scripts/paddle_resolve_orphans.py
    .venv/Scripts/python.exe scripts/paddle_resolve_orphans.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from app.db import service_client
from app.services import paddle_store as ps
from app.services.paddle_orphans import (
    SOURCE_TO_BRAND_SLUG,
    fetch_orphans,
    resolve_from_catalog,
)
from app.services.shopify_catalog import BRANDS_BY_SLUG, to_product_record


# Review source -> the brand catalog its product ids belong to.
async def main() -> int:
    ap = argparse.ArgumentParser(description="Create product rows for orphaned reviews.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = service_client()
    orphans = fetch_orphans(db)
    if not orphans:
        print(json.dumps({"orphan_reviews": 0, "message": "nothing to resolve"}))
        return 0

    brand_ids = ps.brand_id_map(db)
    report: dict[str, Any] = {"per_source": {}, "created": 0, "unresolved": {}}
    new_rows: list[dict[str, Any]] = []

    for source, pids in orphans.items():
        slug = SOURCE_TO_BRAND_SLUG.get(source)
        if not slug:
            report["per_source"][source] = {"orphan_ids": len(pids), "skipped": "no brand catalog"}
            continue
        found = await resolve_from_catalog(slug, pids)
        brand = BRANDS_BY_SLUG[slug]
        for raw in found.values():
            new_rows.append(to_product_record(brand, raw, brand_ids.get(slug)).to_row())
        report["per_source"][source] = {
            "orphan_ids": len(pids), "resolved": len(found), "delisted": len(pids) - len(found),
        }
        report["unresolved"][source] = sorted(pids - set(found))

    if new_rows and not args.dry_run:
        # Stage alongside the catalog so the disk cache stays the source of truth.
        existing = ps.read_staged("products_shopify")
        seen = {(r.get("source"), r.get("source_product_id")) for r in existing}
        merged = existing + [
            r for r in new_rows if (r.get("source"), r.get("source_product_id")) not in seen
        ]
        ps.write_staged("products_shopify", merged)
        ps.upsert_products(db, new_rows)
        report["created"] = len(new_rows)

    report["reviews_linked"] = 0 if args.dry_run else ps.link_reviews_to_products(db)
    report["dry_run"] = args.dry_run
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
