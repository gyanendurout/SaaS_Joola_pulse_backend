"""Adopt reviews whose product row is missing from the catalog.

Bazaarvoice `productid` filtering is family-expanded, so a query for one paddle
returns reviews belonging to sibling variants. Some of those siblings are
colourway products (e.g. "Hyperion CFS 16mm - Vice Blue") that live OUTSIDE
`/collections/pickleball-paddles`, so the catalog stage never saw them and the
review has no `product_id` to point at.

This resolves each orphan id against the brand's *full* `products.json` and
creates the missing `paddle_products` row. Ids that no longer resolve are
delisted; their reviews stay unlinked but keep `brand`, `canonical_name` and
`family_id`, so they remain usable in analytics.

Lives in `app/services/` rather than `scripts/` because the orchestrator needs to
import it — `scripts/` is not a package, so importing from there fails with
"No module named 'scripts'" the moment the pipeline runs as a script.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from app.services import paddle_store as ps
from app.services.shopify_catalog import BRANDS_BY_SLUG, UA, to_product_record

log = structlog.get_logger()

# Review source -> the brand catalog its product ids belong to.
SOURCE_TO_BRAND_SLUG: dict[str, str] = {
    "bazaarvoice": "joola",
    "okendo": "selkirk",
}
MAX_CATALOG_PAGES = 8
INTER_PAGE_SLEEP = 0.4


def fetch_orphans(db: Any) -> dict[str, set[str]]:
    """source -> set of source_product_ids that have reviews but no product row."""
    rows = ps.fetch_all_rows(
        db, "paddle_reviews", "source, source_product_id",
        modifier=lambda q: q.is_("product_id", "null"),
    )
    out: dict[str, set[str]] = {}
    for r in rows:
        pid = str(r.get("source_product_id") or "")
        if pid:
            out.setdefault(r["source"], set()).add(pid)
    return out


async def resolve_from_catalog(
    brand_slug: str, wanted: set[str]
) -> dict[str, dict[str, Any]]:
    """Walk the brand's full products.json — not just the paddle collection."""
    brand = BRANDS_BY_SLUG[brand_slug]
    found: dict[str, dict[str, Any]] = {}
    headers = {"User-Agent": UA, "Accept": "application/json"}
    async with httpx.AsyncClient(
        timeout=30.0, headers=headers, follow_redirects=True
    ) as client:
        for page in range(1, MAX_CATALOG_PAGES + 1):
            resp = await client.get(
                f"{brand.storefront}/products.json?limit=250&page={page}"
            )
            if resp.status_code != 200:
                break
            batch = (resp.json() or {}).get("products") or []
            if not batch:
                break
            for p in batch:
                pid = str(p.get("id") or "")
                if pid in wanted:
                    found[pid] = p
            if len(found) == len(wanted):
                break
            await asyncio.sleep(INTER_PAGE_SLEEP)
    return found


async def adopt_orphan_products(db: Any) -> dict[str, Any]:
    """Create the missing product rows. Returns a per-source report."""
    orphans = fetch_orphans(db)
    if not orphans:
        return {"created": 0, "per_source": {}}

    brand_ids = ps.brand_id_map(db)
    report: dict[str, Any] = {"created": 0, "per_source": {}}
    new_rows: list[dict[str, Any]] = []

    for source, pids in orphans.items():
        slug = SOURCE_TO_BRAND_SLUG.get(source)
        if not slug:
            report["per_source"][source] = {"orphan_ids": len(pids), "skipped": "no catalog"}
            continue
        try:
            found = await resolve_from_catalog(slug, pids)
        except Exception as e:
            log.warning("orphan_resolve_failed", source=source, error=str(e)[:200])
            report["per_source"][source] = {"orphan_ids": len(pids), "error": str(e)[:160]}
            continue

        brand = BRANDS_BY_SLUG[slug]
        for raw in found.values():
            new_rows.append(to_product_record(brand, raw, brand_ids.get(slug)).to_row())
        report["per_source"][source] = {
            "orphan_ids": len(pids),
            "resolved": len(found),
            "delisted": len(pids) - len(found),
        }

    if new_rows:
        rows = [_product_row(r) for r in new_rows]
        try:
            ps.upsert_products(db, rows)
            report["created"] = len(rows)
        except Exception as e:
            log.warning("orphan_upsert_failed", error=str(e)[:200])
    return report


def _product_row(row: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "brand_id", "brand", "source", "retailer", "source_product_id", "family_id",
        "canonical_name", "title", "handle", "product_url", "image_url", "price",
        "currency", "review_count", "avg_rating", "rating_distribution", "gtin",
        "is_paddle", "is_active", "last_seen_at",
    }
    return {k: v for k, v in row.items() if k in allowed}
