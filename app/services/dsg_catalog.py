"""Dick's Sporting Goods catalog — loaded from the robots-declared sitemap harvest.

DSG's storefront HTML is behind an Akamai edge block that 403s even a genuine
headless Chromium. We do **not** attempt to evade that (PADDLE_REVIEWS_PLAN.md
decisions D14/D17): a 403 to a real browser is an enforced access control.

Their `robots.txt`, however, returns 200, declares
`Sitemap: https://www.dickssportinggoods.com/seo_sitemap.xml`, and places no
`Disallow` on `/p/` or `/f/`. Harvesting that advertised sitemap chain yielded
825 products / 321 pickleball paddles with their `dsg_product_id`s — the value a
Bazaarvoice query consumes — with no blocked request involved (D15).

That harvest is checked in at `scripts/_dsg_paddle_products.json`. This module
only loads and normalises it; it makes no network call.

Review TEXT for these products stays gated on Open Question O3: it needs DSG's
public Bazaarvoice client id + `Bv-Bfd-Token`, which live in one PDP's HTML.
Obtaining them costs one page fetch through Apify (`APIFY_ENABLED` is false and
flipping it is an explicit human decision, since it spends third-party credit).
So this stage contributes the catalog only, and is off by default.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import structlog

from app.services.paddle_store import ProductRecord, detect_brand
from app.services.shopify_catalog import is_accessory_title

log = structlog.get_logger()

HARVEST_FILE = Path(__file__).resolve().parents[2] / "scripts" / "_dsg_paddle_products.json"

SOURCE = "dicks"
RETAILER = "Dick's Sporting Goods"

# The harvest stores brand as null and encodes it in the slug instead.
_BRAND_SLUG_HINTS: dict[str, str] = {
    "joola": "JOOLA",
    "selkirk": "Selkirk",
    "paddletek": "Paddletek",
    "crbn": "CRBN",
    "sixzero": "Six Zero",
    "six-zero": "Six Zero",
}


def _title_from_slug(slug: str, dsg_id: str) -> str:
    """Recover a human title from the URL slug.

    Slugs are `{words}-{dsg_product_id}`, e.g.
    `wilson-vesper-control-17mm-pickleball-paddle-25wilavsprcntrlxxsqs`.
    """
    stem = slug[: -(len(dsg_id) + 1)] if dsg_id and slug.endswith(dsg_id) else slug
    words = [w for w in re.split(r"[-_]+", stem) if w]
    return " ".join(w.capitalize() if w.islower() else w for w in words)


def _brand_from_slug(slug: str) -> tuple[str, str | None]:
    """Tracked-brand detection from the slug, falling back to title matching."""
    low = slug.lower()
    for hint, display in _BRAND_SLUG_HINTS.items():
        if hint in low:
            slug_key = "six-zero" if display == "Six Zero" else display.lower()
            return display, slug_key
    return detect_brand(low)


def load_harvest() -> list[dict[str, Any]]:
    """Raw records from the checked-in sitemap harvest. No network access."""
    if not HARVEST_FILE.exists():
        log.warning("dsg_harvest_missing", path=str(HARVEST_FILE))
        return []
    payload = json.loads(HARVEST_FILE.read_text(encoding="utf-8"))
    products = payload.get("products") if isinstance(payload, dict) else payload
    return list(products or [])


def to_product_records(
    brand_ids: dict[str, str] | None = None, *, tracked_only: bool = False
) -> list[ProductRecord]:
    """Normalise the harvest into `paddle_products` rows.

    `tracked_only=True` keeps just the five brands we follow; the default keeps
    every paddle so the retailer feed can also show who else sits on the shelf.
    """
    brand_ids = brand_ids or {}
    out: list[ProductRecord] = []
    for rec in load_harvest():
        if not rec.get("is_paddle"):
            continue
        dsg_id = str(rec.get("dsg_product_id") or "")
        if not dsg_id:
            continue
        slug = str(rec.get("slug") or "")
        title = _title_from_slug(slug, dsg_id)
        # The harvest's own is_paddle flag is loose — it accepts "…Paddle Tape"
        # and "…Paddle Cover". Re-apply the catalog accessory veto.
        if is_accessory_title(title):
            continue
        display, brand_slug = _brand_from_slug(slug)
        if tracked_only and display == "other":
            continue
        out.append(
            ProductRecord(
                source=SOURCE,
                source_product_id=dsg_id,
                brand=display,
                brand_id=brand_ids.get(brand_slug or ""),
                retailer=RETAILER,
                canonical_name=title or None,
                title=title or None,
                handle=slug or None,
                product_url=rec.get("url"),
                is_paddle=True,
                is_active=True,
            )
        )
    log.info("dsg_catalog_loaded", paddles=len(out), tracked_only=tracked_only)
    return out


def brand_breakdown() -> dict[str, int]:
    """Paddle count per tracked brand — the §2.6 table, recomputed from source."""
    counts: dict[str, int] = {}
    for rec in to_product_records():
        counts[rec.brand] = counts.get(rec.brand, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
