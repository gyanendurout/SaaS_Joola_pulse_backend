"""Shopify catalog fetcher for the five tracked paddle brands.

Every brand storefront in scope is Shopify, so `products.json` gives the whole
catalog as clean JSON with no auth (PADDLE_REVIEWS_PLAN.md §2.1, §2.4).

Verified live 2026-08-18: JOOLA 77 · Selkirk 46 · Paddletek 63 · CRBN 85 ·
Six Zero 54 paddle-store products.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.services.paddle_store import ProductRecord

log = structlog.get_logger()

UA = "JoolaPaddleBot/1.0 (+internal; respects robots.txt)"
REQUEST_TIMEOUT = 30.0
INTER_REQUEST_SLEEP = 0.5


@dataclass(frozen=True)
class ShopifyBrand:
    slug: str
    display: str
    source: str                 # paddle_products.source value
    storefront: str             # https://…  (used as Referer for review widgets)
    myshopify: str              # {shop}.myshopify.com — Judge.me needs this
    collection_paths: tuple[str, ...]
    # True when collection_paths point at a paddles-only collection. Those
    # catalogs list paddles whose titles never say "paddle" (e.g. "SLK ERA
    # Power"), so requiring the keyword there silently drops real products.
    curated_collection: bool = False


BRANDS: tuple[ShopifyBrand, ...] = (
    ShopifyBrand(
        slug="joola", display="JOOLA", source="joola_shopify",
        storefront="https://joola.com", myshopify="joola-usa.myshopify.com",
        collection_paths=("/collections/pickleball-paddles/products.json",),
        curated_collection=True,
    ),
    ShopifyBrand(
        slug="selkirk", display="Selkirk", source="selkirk_shopify",
        storefront="https://www.selkirk.com", myshopify="selkirk-sport.myshopify.com",
        collection_paths=("/collections/pickleball-paddles/products.json",),
        curated_collection=True,
    ),
    ShopifyBrand(
        slug="paddletek", display="Paddletek", source="paddletek_shopify",
        storefront="https://www.paddletek.com", myshopify="paddletek-2.myshopify.com",
        collection_paths=("/collections/all/products.json",),
    ),
    ShopifyBrand(
        slug="crbn", display="CRBN", source="crbn_shopify",
        storefront="https://crbnpickleball.com", myshopify="crbn-pickleball.myshopify.com",
        collection_paths=("/collections/all/products.json",),
    ),
    ShopifyBrand(
        slug="six-zero", display="Six Zero", source="sixzero_shopify",
        storefront="https://sixzeropickleball.com", myshopify="six-zero-7668.myshopify.com",
        collection_paths=("/products.json", "/collections/all/products.json"),
    ),
)

BRANDS_BY_SLUG: dict[str, ShopifyBrand] = {b.slug: b for b in BRANDS}

# A "paddle" for our purposes. Brands whose catalog is /collections/all need
# this filter; a dedicated paddle collection does not, but running it anyway is
# harmless and keeps one code path.
_PADDLE_RE = re.compile(r"\bpaddles?\b", re.IGNORECASE)

# Word-boundary matched. Substring matching is wrong here: "ball" is inside
# "Pickleball" and "cap" is inside "capacity", which silently dropped every
# real paddle on the first run.
_NOT_PADDLE_RE = re.compile(
    r"\b("
    r"covers?|cases?|bags?|backpacks?|duffels?|slings?|grips?|overgrips?"
    r"|balls?|shoes?|socks?|hats?|caps?|visors?|shirts?|tees?|tanks?"
    r"|shorts?|skirts?|dress(es)?|jackets?|hoodies?|sweatshirts?|sweatpants?"
    r"|gloves?|towels?|nets?|gift ?cards?|stickers?|decals?|lead tape"
    r"|erasers?|cleaners?|sleeves?|wristbands?|headbands?|apparel"
    r"|bottles?|keychains?|posters?|hats|tape"
    r")\b",
    re.IGNORECASE,
)


def _is_paddle(product: dict[str, Any], *, curated: bool = False) -> bool:
    """Classify one Shopify product as a paddle.

    `curated=True` means the source collection is already paddles-only, so the
    "paddle" keyword is not required — only the accessory veto applies.
    """
    title = product.get("title") or ""
    ptype = product.get("product_type") or ""
    raw_tags = product.get("tags")
    tags = " ".join(raw_tags) if isinstance(raw_tags, list) else str(raw_tags or "")

    # A paddle cover is not a paddle. Judge the veto on title + product_type
    # only — tags on a real paddle often name accessories as cross-sells.
    if _NOT_PADDLE_RE.search(title) or _NOT_PADDLE_RE.search(ptype):
        return False
    if curated or _PADDLE_RE.search(ptype):
        return True
    return bool(_PADDLE_RE.search(f"{title} {ptype} {tags}"))


def is_accessory_title(text: str | None) -> bool:
    """True when a title names an accessory rather than a paddle.

    Exposed because retailer feeds (Dick's, Pickleball Central) carry the same
    "paddle cover / paddle tape is not a paddle" problem and must not each
    re-derive the veto list.
    """
    return bool(text) and bool(_NOT_PADDLE_RE.search(text))


def _first_variant(product: dict[str, Any]) -> dict[str, Any]:
    variants = product.get("variants") or []
    return variants[0] if variants else {}


def _price(product: dict[str, Any]) -> float | None:
    raw = _first_variant(product).get("price")
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _image(product: dict[str, Any]) -> str | None:
    img = product.get("images") or []
    if img and isinstance(img[0], dict):
        return img[0].get("src")
    feat = product.get("featured_image")
    return feat if isinstance(feat, str) else None


def _gtins(product: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for v in product.get("variants") or []:
        bc = v.get("barcode")
        if bc:
            out.append(str(bc))
    return out


def to_product_record(
    brand: ShopifyBrand, product: dict[str, Any], brand_id: str | None
) -> ProductRecord:
    pid = str(product.get("id") or "")
    handle = product.get("handle") or ""
    return ProductRecord(
        source=brand.source,
        source_product_id=pid,
        brand=brand.display,
        brand_id=brand_id,
        retailer=None,
        canonical_name=(product.get("title") or "").strip() or None,
        title=(product.get("title") or "").strip() or None,
        handle=handle or None,
        product_url=f"{brand.storefront}/products/{handle}" if handle else None,
        image_url=_image(product),
        price=_price(product),
        currency="USD",
        gtin=_gtins(product),
        is_paddle=_is_paddle(product, curated=brand.curated_collection),
        is_active=True,
    )


async def fetch_brand_catalog(
    client: httpx.AsyncClient, brand: ShopifyBrand, *, paddles_only: bool = True
) -> list[dict[str, Any]]:
    """Return raw Shopify product dicts for one brand.

    Walks `?limit=250&page=N` until a short page comes back. Shopify caps
    `limit` at 250 and returns an empty `products` array past the end.
    """
    seen_ids: set[str] = set()
    products: list[dict[str, Any]] = []

    for path in brand.collection_paths:
        page = 1
        while page <= 12:                      # 3,000 products is far past any of these catalogs
            url = f"{brand.storefront}{path}?limit=250&page={page}"
            resp = await client.get(url)
            if resp.status_code != 200:
                log.warning(
                    "shopify_catalog_http", brand=brand.slug, path=path,
                    page=page, status=resp.status_code,
                )
                break
            batch = (resp.json() or {}).get("products") or []
            if not batch:
                break
            for p in batch:
                pid = str(p.get("id") or "")
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    products.append(p)
            if len(batch) < 250:
                break
            page += 1
            await asyncio.sleep(INTER_REQUEST_SLEEP)
        if products:
            break                              # first working collection path wins

    if paddles_only:
        products = [p for p in products if _is_paddle(p, curated=brand.curated_collection)]
    log.info("shopify_catalog", brand=brand.slug, products=len(products))
    return products


async def fetch_all_catalogs(
    *, slugs: list[str] | None = None, paddles_only: bool = True
) -> dict[str, list[dict[str, Any]]]:
    """Fetch every (or a subset of) brand catalog. Returns slug -> raw products."""
    targets = [b for b in BRANDS if not slugs or b.slug in slugs]
    out: dict[str, list[dict[str, Any]]] = {}
    headers = {"User-Agent": UA, "Accept": "application/json"}
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, headers=headers, follow_redirects=True
    ) as client:
        for brand in targets:
            try:
                out[brand.slug] = await fetch_brand_catalog(
                    client, brand, paddles_only=paddles_only
                )
            except Exception as e:
                log.warning("shopify_catalog_failed", brand=brand.slug, error=str(e)[:200])
                out[brand.slug] = []
            await asyncio.sleep(INTER_REQUEST_SLEEP)
    return out
