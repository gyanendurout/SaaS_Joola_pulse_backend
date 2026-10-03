"""Bazaarvoice review client for joola.com (BV client id `JOOLA`).

JOOLA's storefront is Shopify with a client-side Bazaarvoice widget, so reviews
are only reachable through the widget's own BFD ("BV front-end data") gateway —
a static HTML fetch of a product page sees nothing at all.

Two access requirements are hard-coded here because both fail loudly rather than
returning an empty result:

  * ``Bv-Bfd-Token`` — a static deployment descriptor (displaycode, zone,
    locale). Not a secret, no expiry, no session binding. Missing => 400.
  * ``Origin: https://joola.com`` — the gateway enforces a CORS-style origin
    check server-side. Missing => 401, *even with a valid token*. This is not
    in PADDLE_REVIEWS_PLAN.md §2.1, which lists only ``Referer``; ``Referer``
    on its own is not sufficient.

Two counting hazards, both handled by deduping on review ``Id``:

  * ``filter=productid:eq:X`` is **family-expanded** — one product query returns
    reviews belonging to sibling variants sharing a ``BV_WB_FAMILY``, so
    querying every variant returns overlapping sets.
  * the summary endpoint's ``numReviews`` also counts **ratings-only**
    submissions (a star with no text), so it is always >= what ``reviews.json``
    returns under ``isratingsonly:eq:false``.

Never sum per-product totals; report unique review ids.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from app.services.paddle_store import ReviewRecord

log = structlog.get_logger()

SOURCE = "bazaarvoice"
BRAND = "JOOLA"
STOREFRONT = "https://joola.com"

BV_CLIENT = "JOOLA"
BV_BASE = (
    "https://apps.bazaarvoice.com/bfd/v1/clients/"
    f"{BV_CLIENT}/api-products/cv2/resources/data"
)
BV_BFD_TOKEN = "21461_3_0,shopify,en_US"
BV_DISPLAY_CODE = "21461_3_0-en_us"
BV_API_VERSION = "5.5"

PAGE_LIMIT = 100            # verified working; the API caps above this
REQUEST_TIMEOUT = 30.0
INTER_REQUEST_SLEEP = 0.45  # 0.35-1.0s band hit zero blocks during research
MAX_PAGES = 60              # guard against a pathological TotalResults
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def bv_headers() -> dict[str, str]:
    """Headers every BFD call needs. See the module docstring for why."""
    return {
        "Bv-Bfd-Token": BV_BFD_TOKEN,
        "Origin": STOREFRONT,
        "Referer": f"{STOREFRONT}/",
        "Accept": "application/json",
        "User-Agent": UA,
    }


def _unwrap(payload: Any) -> dict[str, Any]:
    """BFD nests the classic BV envelope under a ``response`` key.

    Both shapes are tolerated so a gateway revert to the flat form cannot
    silently yield zero reviews.
    """
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("response")
    return inner if isinstance(inner, dict) else payload


# ============================================================================ #
# Requests                                                                       #
# ============================================================================ #

def review_params(
    product_id: str, *, offset: int, limit: int = PAGE_LIMIT
) -> list[tuple[str, str]]:
    """Query for ``reviews.json``.

    A list of tuples, not a dict: the API needs ``filter`` repeated three times
    and a dict would collapse them to one.
    """
    return [
        ("resource", "reviews"),
        ("action", "REVIEWS_N_STATS"),
        ("filter", f"productid:eq:{product_id}"),
        ("filter", "contentlocale:eq:en*,en_US,en_US"),
        ("filter", "isratingsonly:eq:false"),
        ("include", "authors,products,comments"),
        ("filteredstats", "reviews"),
        ("Stats", "Reviews"),
        ("limit", str(limit)),
        ("offset", str(offset)),
        ("sort", "submissiontime:desc"),
        ("apiversion", BV_API_VERSION),
        ("displaycode", BV_DISPLAY_CODE),
    ]


@dataclass
class ProductSummary:
    """Family-level aggregate from the summary endpoint (ratings-only included)."""

    num_reviews: int = 0
    average: float | None = None
    distribution: dict[str, int] = field(default_factory=dict)


async def fetch_summary(
    client: httpx.AsyncClient, product_id: str
) -> ProductSummary | None:
    """``numReviews``, ``average`` and the 5->1 star distribution for a product.

    ``contenttype`` and ``rev`` are both mandatory (the API 400s naming whichever
    is missing), and ``reviewdistribution=primaryRating`` is what turns the star
    histogram on — without it only the average comes back.
    """
    params = {
        "productid": product_id,
        "rev": "0",
        "contenttype": "reviews",
        "reviewdistribution": "primaryRating",
        "contentlocale": "en_US",
        "apiversion": BV_API_VERSION,
        "displaycode": BV_DISPLAY_CODE,
    }
    r = await client.get(f"{BV_BASE}/display/0.2alpha/product/summary", params=params)
    if r.status_code != 200:
        log.warning("bv_summary_failed", pid=product_id, status=r.status_code)
        return None
    summary = _unwrap(r.json()).get("reviewSummary") or {}
    primary = summary.get("primaryRating") or {}
    distribution = {
        str(bucket.get("key")): int(bucket.get("count") or 0)
        for bucket in (primary.get("distribution") or [])
        if bucket.get("key") is not None
    }
    return ProductSummary(
        num_reviews=int(summary.get("numReviews") or 0),
        average=primary.get("average"),
        distribution=distribution,
    )


@dataclass
class ProductScrape:
    """Raw outcome of one product's review walk."""

    source_product_id: str
    reviews: list[dict[str, Any]] = field(default_factory=list)
    products: dict[str, dict[str, Any]] = field(default_factory=dict)
    summary: ProductSummary | None = None
    total_results: int = 0
    error: str | None = None


async def fetch_reviews(client: httpx.AsyncClient, product_id: str) -> ProductScrape:
    """Walk ``offset`` in ``PAGE_LIMIT`` steps until ``TotalResults`` is covered.

    Also terminates on an empty page, so a result set shrinking mid-walk (a
    moderation removal) cannot spin the loop.
    """
    out = ProductScrape(source_product_id=product_id)
    offset = 0
    for _ in range(MAX_PAGES):
        r = await client.get(
            f"{BV_BASE}/reviews.json", params=review_params(product_id, offset=offset)
        )
        if r.status_code != 200:
            out.error = f"HTTP {r.status_code}"
            log.warning(
                "bv_reviews_failed", pid=product_id, status=r.status_code,
                body=r.text[:200],
            )
            return out
        body = _unwrap(r.json())
        page = body.get("Results") or []
        out.total_results = int(body.get("TotalResults") or 0)
        out.products.update((body.get("Includes") or {}).get("Products") or {})
        out.reviews.extend(page)
        offset += PAGE_LIMIT
        if not page or offset >= out.total_results:
            return out
        await asyncio.sleep(INTER_REQUEST_SLEEP)
    log.warning("bv_pagination_capped", pid=product_id, got=len(out.reviews))
    return out


# ============================================================================ #
# Mapping                                                                        #
# ============================================================================ #

def _media_urls(raw: dict[str, Any]) -> list[str]:
    """Largest available still per photo, plus any video URL we can locate."""
    urls: list[str] = []
    for photo in raw.get("Photos") or []:
        sizes = photo.get("Sizes") or {}
        for key in ("large", "normal", "thumbnail"):
            url = (sizes.get(key) or {}).get("Url")
            if url:
                urls.append(url)
                break
    for video in raw.get("Videos") or []:
        url = video.get("VideoUrl") or video.get("Url") or video.get("Uri")
        if url:
            urls.append(url)
    return urls


def _flatten_values(bag: dict[str, Any] | None) -> dict[str, Any] | None:
    """``{"Age": {"Value": "35to44", ...}}`` -> ``{"Age": "35to44"}``.

    BV wraps every secondary rating and context value in a descriptor object;
    flattening keeps the JSONB column queryable without a nested path.
    """
    if not bag:
        return None
    return {k: (v.get("Value") if isinstance(v, dict) else v) for k, v in bag.items()}


def _family_id(raw: dict[str, Any], products: dict[str, dict[str, Any]]) -> str | None:
    """``BV_WB_FAMILY`` for the review's own product, e.g. ``KosmosV``.

    Stored so a paddle family can be rolled up without trusting any single
    variant's review count.
    """
    meta = products.get(str(raw.get("ProductId") or "")) or {}
    families = meta.get("FamilyIds") or []
    if families:
        return str(families[0])
    attr = (meta.get("Attributes") or {}).get("BV_WB_FAMILY") or {}
    values = attr.get("Values") or []
    return str(values[0].get("Value")) if values else None


def to_review_record(
    raw: dict[str, Any],
    *,
    products: dict[str, dict[str, Any]],
    catalog: dict[str, dict[str, Any]],
    queried_product: dict[str, Any],
) -> ReviewRecord:
    """Map one BV review onto the unified ``ReviewRecord``.

    ``queried_product`` is the catalog row we asked about. Because the API is
    family-expanded the review may really belong to a sibling variant, so the
    sibling's own catalog row wins for name/brand attribution when we have it.
    """
    own_pid = str(raw.get("ProductId") or "") or str(
        queried_product.get("source_product_id") or ""
    )
    product = catalog.get(own_pid) or queried_product
    responses = raw.get("ClientResponses") or []
    reply = responses[0] if responses else {}

    return ReviewRecord(
        source=SOURCE,
        external_review_id=str(raw.get("Id")),
        source_product_id=own_pid or None,
        brand=BRAND,
        brand_id=product.get("brand_id"),
        family_id=_family_id(raw, products),
        canonical_name=product.get("canonical_name") or raw.get("OriginalProductName"),
        reviewer_name=raw.get("UserNickname"),          # nullable by design (§8)
        reviewer_location=raw.get("UserLocation"),
        rating=raw.get("Rating"),
        title=raw.get("Title"),
        body=raw.get("ReviewText"),
        pros=raw.get("Pros"),
        cons=raw.get("Cons"),
        secondary_ratings=_flatten_values(raw.get("SecondaryRatings")),
        context_values=_flatten_values(raw.get("ContextDataValues")),
        posted_at=raw.get("SubmissionTime"),
        is_verified="verifiedPurchaser" in (raw.get("Badges") or {}),
        is_recommended=raw.get("IsRecommended"),
        is_syndicated=bool(raw.get("IsSyndicated")),
        helpful_count=int(raw.get("TotalPositiveFeedbackCount") or 0),
        unhelpful_count=int(raw.get("TotalNegativeFeedbackCount") or 0),
        brand_response=reply.get("Response") or None,
        brand_response_at=reply.get("Date") or None,
        media_urls=_media_urls(raw),
        language_code=raw.get("ContentLocale"),
    )


# ============================================================================ #
# Orchestration                                                                  #
# ============================================================================ #

@dataclass
class BazaarvoiceResult:
    records: list[ReviewRecord] = field(default_factory=list)
    product_stats: list[dict[str, Any]] = field(default_factory=list)
    families: set[str] = field(default_factory=set)
    errors: list[dict[str, str]] = field(default_factory=list)
    products_scraped: int = 0


async def scrape_catalog(
    catalog_rows: list[dict[str, Any]], *, with_summary: bool = True
) -> BazaarvoiceResult:
    """Scrape every supplied JOOLA product, deduped on review Id across the run.

    Dedupe is run-wide rather than per-product because ``productid`` filtering is
    family-expanded: sibling variants return overlapping reviews and a naive
    concatenation over-counts (Perseus Pro IV appears 6x, each reporting 291).
    """
    catalog = {
        str(row.get("source_product_id")): row
        for row in catalog_rows
        if row.get("source_product_id")
    }
    result = BazaarvoiceResult()
    seen: set[str] = set()

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers=bv_headers()) as client:
        for pid, row in catalog.items():
            scrape = await fetch_reviews(client, pid)
            result.products_scraped += 1
            if scrape.error:
                result.errors.append({"source_product_id": pid, "error": scrape.error})
            elif with_summary:
                await asyncio.sleep(INTER_REQUEST_SLEEP)
                scrape.summary = await fetch_summary(client, pid)

            _collect(result, scrape, catalog=catalog, queried=row, seen=seen)
            log.info(
                "bv_product_done", pid=pid, page_reviews=len(scrape.reviews),
                unique_total=len(seen),
            )
            await asyncio.sleep(INTER_REQUEST_SLEEP)
    return result


def _collect(
    result: BazaarvoiceResult,
    scrape: ProductScrape,
    *,
    catalog: dict[str, dict[str, Any]],
    queried: dict[str, Any],
    seen: set[str],
) -> None:
    """Fold one product's scrape into the run-level result: dedupe plus stats."""
    ratings: list[int] = []
    family: str | None = None
    for raw in scrape.reviews:
        record = to_review_record(
            raw, products=scrape.products, catalog=catalog, queried_product=queried
        ).finalise()
        family = family or record.family_id
        if record.rating is not None:
            ratings.append(int(record.rating))
        if record.external_review_id in seen:
            continue
        seen.add(record.external_review_id)
        result.records.append(record)
        if record.family_id:
            result.families.add(record.family_id)

    summary = scrape.summary
    fallback_avg = round(sum(ratings) / len(ratings), 2) if ratings else None
    result.product_stats.append({
        "source": SOURCE,
        "source_product_id": scrape.source_product_id,
        # Summary counts ratings-only submissions too, so it is the honest
        # storefront-facing total; text_review_count is what we can read.
        "review_count": summary.num_reviews if summary else len(scrape.reviews),
        "avg_rating": (
            summary.average
            if summary and summary.average is not None
            else fallback_avg
        ),
        "rating_distribution": summary.distribution if summary else {},
        "family_id": family,
        "text_review_count": scrape.total_results,
    })
