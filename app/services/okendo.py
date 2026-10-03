"""Okendo review client — selkirk.com (PADDLE_REVIEWS_PLAN.md §2.2).

Okendo exposes a fully public, cursor-paged JSON review API. No auth header,
no cookie, no token to re-derive. One product id per call:

    GET {API_BASE}/stores/{storeId}/products/shopify-{shopifyProductId}/reviews?limit=100

The single non-obvious behaviour — and the reason this module exists instead of
inlining two `httpx.get` calls — is documented on `resolve_next_url` below.
It costs a hard 403 if a caller gets it wrong, so it lives in one pure,
unit-tested function (`tests/test_okendo_url.py`, plan §8).
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog

from app.services.paddle_store import ReviewRecord

log = structlog.get_logger()

SOURCE = "okendo"
BRAND = "Selkirk"

# Public store id for selkirk.com. Not a secret — it is embedded in the
# storefront's own client-side widget bundle.
SELKIRK_STORE_ID = "51eb4f5f-5c6e-4e06-9280-1145c4fb7894"

API_HOST = "api.okendo.io"
API_ROOT = f"https://{API_HOST}"
API_VERSION_PREFIX = "/v1"
API_BASE = f"{API_ROOT}{API_VERSION_PREFIX}"

UA = "JoolaPaddleBot/1.0 (+internal; respects robots.txt)"
REQUEST_TIMEOUT = 30.0
PAGE_LIMIT = 100

# Politeness window verified in research: zero blocks hit at this rate (§8).
SLEEP_MIN = 0.35
SLEEP_MAX = 1.0

# A malformed or self-referential `nextUrl` must never spin forever. At
# PAGE_LIMIT=100 this ceiling still allows 20k reviews on a single product,
# far above the busiest Selkirk paddle observed (341).
MAX_PAGES_PER_PRODUCT = 200

# Okendo moderation states that mean "visible on the storefront". Anything
# else (pending, rejected, spam, ...) is dropped and counted, never stored.
PUBLISHED_STATUSES: frozenset[str] = frozenset({"approved", "published", "public"})


# ============================================================================ #
# URL handling — the one gotcha                                                  #
# ============================================================================ #

def reviews_url(store_id: str, source_product_id: str, limit: int = PAGE_LIMIT) -> str:
    """First-page URL for one Shopify product."""
    return (
        f"{API_BASE}/stores/{store_id}"
        f"/products/shopify-{source_product_id}/reviews?limit={limit}"
    )


def resolve_next_url(next_url: str | None) -> str | None:
    """Turn Okendo's `nextUrl` into an absolute, fetchable URL.

    WHY THIS IS NOT `urljoin`: Okendo returns `nextUrl` relative to the API
    *root* but strips the `/v1` version segment, e.g.

        "/stores/51eb.../products/shopify-800.../reviews?limit=100&lastEvaluated=..."

    Joining that against `https://api.okendo.io` yields a URL missing `/v1`,
    which the gateway answers with a hard **403 Forbidden** — not a 404, so it
    reads like a blocked scraper rather than a bad path and sends you down the
    wrong debugging road. The version prefix must be re-attached explicitly.

    Returns None when there is no next page, or when the cursor points off-host
    (we follow our own API's pagination, never an arbitrary redirect target).
    """
    if not next_url:
        return None
    raw = next_url.strip()
    if not raw:
        return None

    if raw.startswith(("http://", "https://")):
        # Defensive: honour a fully-qualified cursor only if it stays on-host
        # and already carries the version prefix.
        parts = urlsplit(raw)
        if parts.hostname != API_HOST:
            return None
        return raw if parts.path.startswith(f"{API_VERSION_PREFIX}/") else None

    if not raw.startswith("/"):
        raw = f"/{raw}"
    if raw.startswith(f"{API_VERSION_PREFIX}/"):
        # Already versioned — prefixing again would produce /v1/v1/... (404).
        return f"{API_ROOT}{raw}"
    return f"{API_BASE}{raw}"


# ============================================================================ #
# Field mapping                                                                  #
# ============================================================================ #

def is_published(review: dict[str, Any]) -> bool:
    """True when Okendo's moderation status means the review is live."""
    return str(review.get("status") or "").strip().lower() in PUBLISHED_STATUSES


def _reviewer_name(review: dict[str, Any]) -> str | None:
    """`reviewer` may be absent, and `displayName` may be missing or empty."""
    reviewer = review.get("reviewer")
    if not isinstance(reviewer, dict):
        return None
    name = reviewer.get("displayName")
    if not isinstance(name, str):
        return None
    return name.strip() or None


def _is_verified(review: dict[str, Any]) -> bool | None:
    reviewer = review.get("reviewer")
    if not isinstance(reviewer, dict):
        return None
    flag = reviewer.get("isVerified")
    return bool(flag) if isinstance(flag, bool) else None


def _media_urls(review: dict[str, Any]) -> list[str]:
    """Prefer the largest still image Okendo offers per media item."""
    out: list[str] = []
    for item in review.get("media") or []:
        if not isinstance(item, dict):
            continue
        url = item.get("fullSizeUrl") or item.get("largeUrl") or item.get("thumbnailUrl")
        if isinstance(url, str) and url:
            out.append(url)
    return out


def _secondary_ratings(review: dict[str, Any]) -> dict[str, Any] | None:
    """Okendo's per-attribute sliders (power, control, spin) as title -> value."""
    bag = {
        str(a["title"]): a.get("value")
        for a in review.get("attributesWithRating") or []
        if isinstance(a, dict) and a.get("title") is not None
    }
    return bag or None


def _context_values(review: dict[str, Any]) -> dict[str, Any] | None:
    """Survey answers about the product and the reviewer's play profile.

    Reviewer attributes are merchant-configured, so they are the one place a
    PII field could appear. `ReviewRecord.finalise()` scrubs this bag before it
    is staged (paddle_store._scrub_custom_fields) — do not bypass it.
    """
    bag: dict[str, Any] = {}
    for a in review.get("productAttributes") or []:
        if isinstance(a, dict) and a.get("title") is not None:
            bag[str(a["title"])] = a.get("value")
    reviewer = review.get("reviewer")
    if isinstance(reviewer, dict):
        for a in reviewer.get("attributes") or []:
            if isinstance(a, dict) and a.get("title") is not None:
                bag[f"reviewer.{a['title']}"] = a.get("value")
    return bag or None


def to_review_record(review: dict[str, Any], product: dict[str, Any]) -> ReviewRecord:
    """Map one raw Okendo review onto the shared ReviewRecord shape."""
    rating = review.get("rating")
    return ReviewRecord(
        source=SOURCE,
        external_review_id=str(review.get("reviewId") or ""),
        source_product_id=str(product.get("source_product_id") or ""),
        brand=product.get("brand") or BRAND,
        brand_id=product.get("brand_id"),
        canonical_name=product.get("canonical_name") or product.get("title"),
        reviewer_name=_reviewer_name(review),
        rating=int(rating) if isinstance(rating, (int, float)) else None,
        title=review.get("title") or None,
        body=review.get("body") or None,
        secondary_ratings=_secondary_ratings(review),
        context_values=_context_values(review),
        posted_at=review.get("dateCreated") or None,
        is_verified=_is_verified(review),
        is_recommended=review.get("isRecommended"),
        is_incentivized=review.get("isIncentivized"),
        helpful_count=int(review.get("helpfulCount") or 0),
        unhelpful_count=int(review.get("unhelpfulCount") or 0),
        media_urls=_media_urls(review),
        language_code=review.get("languageCode") or None,
    )


# ============================================================================ #
# Scrape                                                                         #
# ============================================================================ #

@dataclass
class ProductStats:
    source: str
    source_product_id: str
    review_count: int
    avg_rating: float | None


@dataclass
class OkendoScrapeResult:
    reviews: list[ReviewRecord] = field(default_factory=list)
    stats: list[ProductStats] = field(default_factory=list)
    products_scraped: int = 0
    dupes_skipped: int = 0
    non_published_skipped: int = 0
    errors: int = 0

    def summary(self) -> dict[str, int]:
        """Run-level counters. `reviews_unique` is a UNIQUE count by design."""
        return {
            "products_scraped": self.products_scraped,
            "reviews_unique": len(self.reviews),
            "dupes_skipped": self.dupes_skipped,
            "non_published_skipped": self.non_published_skipped,
            "errors": self.errors,
        }


async def _sleep_politely() -> None:
    await asyncio.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))


async def fetch_product_reviews(
    client: httpx.AsyncClient,
    source_product_id: str,
    *,
    store_id: str = SELKIRK_STORE_ID,
    limit: int = PAGE_LIMIT,
    max_pages: int = MAX_PAGES_PER_PRODUCT,
) -> tuple[list[dict[str, Any]], bool]:
    """Walk every page for one product. Returns (raw reviews, ok).

    `ok` is False when the walk ended on an error (including a 403). Access
    controls are recorded and skipped, never worked around (plan §8).
    """
    url: str | None = reviews_url(store_id, source_product_id, limit)
    raw: list[dict[str, Any]] = []
    pages = 0

    while url and pages < max_pages:
        page, next_raw, ok = await _fetch_page(client, url, source_product_id, pages + 1)
        if not ok:
            return raw, False
        raw.extend(page)
        pages += 1

        # An empty page with a cursor still set would otherwise burn the whole
        # page budget for nothing.
        url = resolve_next_url(next_raw) if page else None
        if url:
            await _sleep_politely()

    if url:
        log.warning("okendo_page_ceiling_hit", product=source_product_id, pages=pages)

    return raw, True


async def _fetch_page(
    client: httpx.AsyncClient, url: str, product: str, page_no: int
) -> tuple[list[dict[str, Any]], str | None, bool]:
    """One HTTP hop. Returns (reviews, raw nextUrl, ok)."""
    try:
        resp = await client.get(url)
    except httpx.HTTPError as e:
        log.warning("okendo_request_failed", product=product, error=str(e)[:200])
        return [], None, False
    if resp.status_code != 200:
        log.warning(
            "okendo_bad_status", product=product, status=resp.status_code, page=page_no
        )
        return [], None, False
    try:
        payload = resp.json()
    except ValueError:
        log.warning("okendo_bad_json", product=product, page=page_no)
        return [], None, False
    reviews = [r for r in (payload.get("reviews") or []) if isinstance(r, dict)]
    return reviews, payload.get("nextUrl"), True


async def scrape_products(
    products: list[dict[str, Any]],
    *,
    store_id: str = SELKIRK_STORE_ID,
    limit: int = PAGE_LIMIT,
) -> OkendoScrapeResult:
    """Scrape every product, deduping review ids across the whole run.

    Okendo groups variants under one product id, but a review can still surface
    on more than one catalog row, so the seen-set is run-wide rather than
    per-product — reported counts are always UNIQUE counts (plan §8).
    """
    result = OkendoScrapeResult()
    seen: set[str] = set()
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": "https://www.selkirk.com/",
    }

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, headers=headers, follow_redirects=True
    ) as client:
        for i, product in enumerate(products, start=1):
            pid = str(product.get("source_product_id") or "")
            if not pid:
                result.errors += 1
                continue

            raw, ok = await fetch_product_reviews(client, pid, store_id=store_id, limit=limit)
            if not ok:
                result.errors += 1
            result.products_scraped += 1

            kept = _keep_new(raw, product, seen, result)
            result.reviews.extend(kept)
            result.stats.append(_product_stats(product, kept))
            log.info(
                "okendo_product_done",
                index=i,
                total=len(products),
                product=pid,
                name=product.get("title"),
                reviews=len(kept),
            )
            if i < len(products):
                await _sleep_politely()

    return result


def _keep_new(
    raw: list[dict[str, Any]],
    product: dict[str, Any],
    seen: set[str],
    result: OkendoScrapeResult,
) -> list[ReviewRecord]:
    """Filter to published, not-yet-seen reviews and map them, updating counters."""
    kept: list[ReviewRecord] = []
    for review in raw:
        rid = str(review.get("reviewId") or "")
        if not rid:
            continue
        if not is_published(review):
            result.non_published_skipped += 1
            continue
        if rid in seen:
            result.dupes_skipped += 1
            continue
        seen.add(rid)
        kept.append(to_review_record(review, product).finalise())
    return kept


def _product_stats(product: dict[str, Any], kept: list[ReviewRecord]) -> ProductStats:
    rated = [r.rating for r in kept if r.rating is not None]
    return ProductStats(
        source=str(product.get("source") or "selkirk_shopify"),
        source_product_id=str(product.get("source_product_id") or ""),
        review_count=len(kept),
        avg_rating=round(sum(rated) / len(rated), 2) if rated else None,
    )
