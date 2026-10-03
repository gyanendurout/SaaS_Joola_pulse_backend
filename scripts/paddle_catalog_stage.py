"""Stage 1 — fetch all five Shopify catalogs, stage products to disk (+DB if ready)."""
from __future__ import annotations

import asyncio
import json
import sys

from app.db import service_client
from app.services import paddle_store as ps
from app.services.shopify_catalog import BRANDS, fetch_all_catalogs, to_product_record


async def main() -> int:
    db = service_client()
    bmap = ps.brand_id_map(db)
    cats = await fetch_all_catalogs(paddles_only=True)

    rows: list[dict] = []
    summary: dict[str, int] = {}
    for brand in BRANDS:
        raw = cats.get(brand.slug, [])
        summary[brand.slug] = len(raw)
        for p in raw:
            rows.append(to_product_record(brand, p, bmap.get(brand.slug)).to_row())

    ps.write_staged("products_shopify", rows)
    print(json.dumps({"per_brand": summary, "total_paddles": len(rows)}, indent=2))

    if ps.tables_ready(db):
        n = ps.upsert_products(db, rows)
        print(f"DB: upserted {n} products")
    else:
        print("DB: migration 011 not applied yet -> staged to disk only")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
