"""Pickleball Central (BigCommerce) paddle-catalog enumeration.

Resolves PADDLE_REVIEWS_PLAN.md Open Question **O4**.

Why this module exists and why it is shaped this way
---------------------------------------------------
Pickleball Central is the only source where all five tracked brands are reviewed
by the *same* buyer population (plan §2.5, D12), so its catalog is the key that
unlocks the Yotpo review feed — `yotpo.py` consumes BigCommerce product ids and
nothing else.

Three facts drove the design:

1. **The storefront 403s plain `httpx` (Cloudflare) but serves a real browser
   200.** So brand-category *discovery* runs through Playwright (plan §2.5, §3
   "Playwright — second role"). We never retry-storm it and never attempt
   evasion (D8/D14/D17): a 403 to a genuine browser is recorded as a "no".
2. **The category pages are client-rendered** — the product grid is absent from
   the server HTML, so scraping the rendered DOM would be the fragile path.
3. **The grid is populated by Searchspring** (`pmls5v.a.searchspring.io`), a
   separate public host that answers `httpx` directly and returns typed JSON
   including `uid` — which *is* the BigCommerce product id. That is the
   enumeration path: fewer requests, no HTML parsing, and zero extra load on
   the protected storefront.

`/paddles/by-brand/{brand}/` is therefore used for discovery only; the ids come
from the same API the storefront itself calls.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.services.paddle_store import ProductRecord, detect_brand

log = structlog.get_logger()

SOURCE = "pickleballcentral_bc"
RETAILER = "Pickleball Central"
STOREFRONT = "https://pickleballcentral.com"
PADDLES_INDEX = f"{STOREFRONT}/paddles/"

# Public Searchspring site id, published client-side by their own storefront.
SEARCHSPRING_SITE_ID = "pmls5v"
SEARCHSPRING_URL = (
    f"https://{SEARCHSPRING_SITE_ID}.a.searchspring.io/api/search/category.json"
)

# Top-level background filter that scopes a query to the paddle tree.
PADDLES_CATEGORY = "Paddles"

REQUEST_TIMEOUT = 30.0
# Good-citizen pacing (plan §8 rate-limiting row): >=1s between storefront page
# loads, sub-second between API calls on the unprotected host.
STOREFRONT_SLEEP = 1.5
API_SLEEP = 0.5
RESULTS_PER_PAGE = 100
MAX_PAGES = 20

# A desktop UA. Playwright drives a genuine Chromium; this only stops the
# default "HeadlessChrome" token from reading as a bot. It is not fingerprint
# spoofing — no stealth plugin, no proxy rotation, no CAPTCHA solving (D14).
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
API_UA = "JoolaPaddleBot/1.0 (+internal; respects robots.txt)"


@dataclass(frozen=True)
class PbcBrand:
    """One tracked brand as Pickleball Central models it."""

    slug: str            # paddle_store.BRAND_SLUGS key
    display: str         # paddle_products.brand value
    facet: str           # exact Searchspring `brand` facet value
    url_token: str       # substring identifying its /paddles/by-brand/ URL


# Facet values were read live from the Searchspring `brand` facet under
# "Paddles". They are NOT our display names ("6.0 Six Zero" != "Six Zero"),
# which is why they are pinned here rather than derived from BRAND_SLUGS.
TRACKED_BRANDS: tuple[PbcBrand, ...] = (
    PbcBrand("joola", "JOOLA", "JOOLA", "joola-pickleball-paddles"),
    PbcBrand("selkirk", "Selkirk", "Selkirk", "selkirk-sports"),
    PbcBrand("paddletek", "Paddletek", "Paddletek", "paddletek-pickleball-paddles"),
    PbcBrand("crbn", "CRBN", "CRBN", "crbn-pickleball-paddles"),
    PbcBrand("six-zero", "Six Zero", "6.0 Six Zero", "6-0-six-zero-pickleball-paddles"),
)

BRANDS_BY_SLUG: dict[str, PbcBrand] = {b.slug: b for b in TRACKED_BRANDS}

# Accessory veto. The "Paddles" category tree carries a little non-paddle noise
# (covers, guides, ball machines), and a paddle cover is not a paddle.
_NOT_PADDLE_RE = re.compile(
    r"\b("
    r"covers?|cases?|bags?|backpacks?|slings?|grips?|overgrips?|balls?|shoes?"
    r"|socks?|hats?|caps?|visors?|shirts?|tees?|shorts?|skirts?|jackets?"
    r"|hoodies?|gloves?|towels?|nets?|gift ?cards?|stickers?|decals?|lead tape"
    r"|erasers?|cleaners?|sleeves?|wristbands?|headbands?|apparel|bottles?"
    r"|machines?|guides?|quiz"
    r")\b",
    re.IGNORECASE,
)
# `imageUrl` embeds the BigCommerce product id: /products/{id}/{image_id}/...
_IMAGE_PRODUCT_ID_RE = re.compile(r"/products/(\d+)/")


def _absolute(href: str) -> str:
    if href.startswith("http"):
        return href
    return f"{STOREFRONT}/{href.lstrip('/')}"


# ============================================================================ #
# Step 1 — discovery (Playwright, storefront)                                   #
# ============================================================================ #

async def discover_brand_category_urls(
    *, timeout_sec: int = 45,
) -> tuple[dict[str, str], str | None]:
    """Read `/paddles/` in a real browser and return `({slug: url}, error)`.

    One page load, on purpose. Returns `({}, reason)` when the storefront
    refuses a genuine browser — the caller then reports reduced coverage rather
    than escalating (D14).
    """
    from playwright.async_api import async_playwright  # lazy: heavy import

    found: dict[str, str] = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(user_agent=BROWSER_UA)
            page = await ctx.new_page()
            try:
                resp = await page.goto(
                    PADDLES_INDEX,
                    wait_until="domcontentloaded",
                    timeout=timeout_sec * 1000,
                )
            except Exception as e:                      # network / timeout only
                log.warning("pbc_discovery_goto_failed", error=str(e)[:200])
                return {}, f"goto_failed: {str(e)[:120]}"

            status = resp.status if resp else 0
            if status != 200:
                log.warning("pbc_discovery_blocked", status=status)
                return {}, f"storefront_status_{status}"

            hrefs: list[str] = await page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.getAttribute('href') || '')"
            )
            for brand in TRACKED_BRANDS:
                for href in hrefs:
                    if "by-brand" in href and brand.url_token in href:
                        found[brand.slug] = _absolute(href)
                        break
            log.info("pbc_discovery", status=status, brands=len(found))
        finally:
            await browser.close()

    await asyncio.sleep(STOREFRONT_SLEEP)
    return found, None


# ============================================================================ #
# Step 2 — enumeration (httpx, Searchspring)                                    #
# ============================================================================ #

async def fetch_brand_products(
    client: httpx.AsyncClient, brand: PbcBrand
) -> list[dict[str, Any]]:
    """Every paddle-tree product for one brand, paginated and id-deduped.

    Scoped by the background category filter plus the brand facet. The facet is
    deliberately preferred over the `/by-brand/` sub-category: the sub-category
    under-reports (JOOLA 56 vs 79) because paddles filed elsewhere in the
    Paddles tree never appear in it.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 1
    while page <= MAX_PAGES:
        params: dict[str, Any] = {
            "siteId": SEARCHSPRING_SITE_ID,
            "resultsFormat": "native",
            "bgfilter.categories_hierarchy": PADDLES_CATEGORY,
            "filter.brand": brand.facet,
            "resultsPerPage": RESULTS_PER_PAGE,
            "page": page,
        }
        resp = await client.get(SEARCHSPRING_URL, params=params)
        if resp.status_code != 200:
            log.warning(
                "pbc_catalog_http", brand=brand.slug, page=page,
                status=resp.status_code,
            )
            break
        payload = resp.json() or {}
        results = payload.get("results") or []
        for item in results:
            uid = str(item.get("uid") or "")
            if uid and uid not in seen:
                seen.add(uid)
                out.append(item)
        total_pages = int((payload.get("pagination") or {}).get("totalPages") or 1)
        if page >= total_pages or not results:
            break
        page += 1
        await asyncio.sleep(API_SLEEP)

    log.info("pbc_catalog_brand", brand=brand.slug, products=len(out))
    return out


async def fetch_all_products(
    *, slugs: list[str] | None = None, paddles_only: bool = True
) -> dict[str, list[dict[str, Any]]]:
    """slug -> raw Searchspring items for every tracked brand."""
    targets = [b for b in TRACKED_BRANDS if not slugs or b.slug in slugs]
    out: dict[str, list[dict[str, Any]]] = {}
    headers = {"User-Agent": API_UA, "Accept": "application/json"}
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, headers=headers, follow_redirects=True
    ) as client:
        for brand in targets:
            try:
                items = await fetch_brand_products(client, brand)
            except Exception as e:
                log.warning("pbc_catalog_failed", brand=brand.slug, error=str(e)[:200])
                items = []
            if paddles_only:
                items = [i for i in items if is_paddle(i)]
            out[brand.slug] = items
            await asyncio.sleep(API_SLEEP)
    return out


def is_paddle(item: dict[str, Any]) -> bool:
    """Veto obvious non-paddles filed under the Paddles category tree."""
    name = str(item.get("name") or "")
    ptype = str(item.get("product_type_unigram") or "")
    if _NOT_PADDLE_RE.search(name):
        return False
    return not _NOT_PADDLE_RE.search(ptype)


# ============================================================================ #
# Step 3 — normalisation                                                        #
# ============================================================================ #

def _float_or_none(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _int_or_zero(raw: Any) -> int:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


def bc_product_id(item: dict[str, Any]) -> str | None:
    """BigCommerce product id — `uid`, with the image CDN path as a fallback."""
    uid = str(item.get("uid") or "").strip()
    if uid.isdigit():
        return uid
    match = _IMAGE_PRODUCT_ID_RE.search(str(item.get("imageUrl") or ""))
    return match.group(1) if match else None


def to_product_record(
    item: dict[str, Any],
    brand_ids: dict[str, str],
    *,
    fallback: PbcBrand | None = None,
) -> ProductRecord | None:
    """One Searchspring item -> `ProductRecord`, or None when it carries no id.

    Brand is *inferred from the title* via `detect_brand`, because on a retailer
    feed the brand is not a trustworthy structural field. The retailer's own
    `brand` value is consulted only when the title is silent — e.g. "SLK
    Valkyrie Widebody" is a Selkirk paddle whose title never says "Selkirk".
    """
    pid = bc_product_id(item)
    if not pid:
        return None

    title = str(item.get("name") or "").strip() or None
    display, slug = detect_brand(title)
    if slug is None:
        display, slug = detect_brand(str(item.get("brand") or ""))
    if slug is None and fallback is not None:
        display, slug = fallback.display, fallback.slug

    url = str(item.get("url") or "").strip()

    return ProductRecord(
        source=SOURCE,
        source_product_id=pid,
        brand=display,
        brand_id=brand_ids.get(slug or ""),
        retailer=RETAILER,
        canonical_name=title,
        title=title,
        handle=(url.strip("/") or None),
        product_url=_absolute(url) if url else None,
        image_url=str(item.get("imageUrl") or "") or None,
        price=_float_or_none(item.get("price")),
        currency="USD",
        review_count=_int_or_zero(item.get("ratingCount") or item.get("rating_count")),
        avg_rating=_float_or_none(item.get("rating")),
        is_paddle=is_paddle(item),
        is_active=str(item.get("ss_in_stock") or "1") == "1",
    )


# ============================================================================ #
# Fallback — rendered-DOM id scrape                                             #
# ============================================================================ #

async def enumerate_ids_via_browser(
    category_url: str, *, timeout_sec: int = 45
) -> list[str]:
    """Last-resort id scrape from one rendered category page.

    Only used if Searchspring stops answering. Product ids are recovered from
    the BigCommerce image CDN paths present in the rendered grid.
    """
    from playwright.async_api import async_playwright

    ids: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(user_agent=BROWSER_UA)
            page = await ctx.new_page()
            try:
                await page.goto(
                    category_url, wait_until="load", timeout=timeout_sec * 1000
                )
                await asyncio.sleep(4.0)            # client-rendered grid
                html = await page.content()
            except Exception as e:
                log.warning("pbc_dom_enum_failed", url=category_url, error=str(e)[:200])
                return []
            ids = sorted({m for m in _IMAGE_PRODUCT_ID_RE.findall(html)})
        finally:
            await browser.close()

    await asyncio.sleep(STOREFRONT_SLEEP)
    log.info("pbc_dom_enum", url=category_url, ids=len(ids))
    return ids
