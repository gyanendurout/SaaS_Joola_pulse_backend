"""Judge.me review sourcing for Paddletek, CRBN and Six Zero.

All three brands are Shopify + Judge.me, but they run **two different widget
generations** and the public endpoint answers each differently
(PADDLE_REVIEWS_PLAN.md §2.4):

  * **legacy** (Paddletek, CRBN) — the response carries an ``html`` key holding
    a server-rendered fragment. This is the only source in the pipeline that
    needs DOM parsing rather than typed JSON.
  * **v3 React** (Six Zero) — no ``html`` key at all, only the aggregate
    summary (``number_of_reviews`` / ``average_rating`` / ``histogram``).

Detecting the missing ``html`` key and degrading to ratings-only is therefore a
correctness requirement, not defensive padding: the two generations share one
URL and only differ in response shape.

RESOLVED (2026-08-18) — where the star rating actually lives
------------------------------------------------------------
The plan recorded the rating as unfindable because ``data-score`` and
``aria-label`` regexes came back empty. Both were being matched in the wrong
place and the wrong way. On the live fragment the rating sits on a **nested**
element, not on the ``.jdgm-rev`` review root, and Judge.me emits
**single-quoted** attributes::

    <span class='jdgm-rev__rating' data-score='5'
          aria-label='5 star review' role='img'>
      <span class='jdgm-star jdgm--on'></span>  x5
    </span>

So there are three independent carriers of the same number, tried in order by
:func:`parse_rating`: ``.jdgm-rev__rating[data-score]``, then the
``aria-label='N star review'`` text, then a count of lit ``.jdgm-star.jdgm--on``
children. A regex over the raw HTML expecting double quotes finds none of them.

Failure policy (plan §8): a rating that cannot be parsed raises
:class:`RatingParseError`. Judge.me changing its markup must break the run
loudly rather than quietly persisting a column of NULL ratings that later reads
as "these customers had no opinion".
"""
from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from bs4 import BeautifulSoup, Tag

from app.services.paddle_store import ReviewRecord, content_hash
from app.services.shopify_catalog import BRANDS_BY_SLUG, ShopifyBrand

log = structlog.get_logger()

WIDGET_URL = "https://judge.me/reviews/reviews_for_widget"
SOURCE = "judgeme"
USER_AGENT = "JoolaPaddleBot/1.0 (+internal; respects robots.txt)"
REQUEST_TIMEOUT = 30.0
PER_PAGE = 10                 # the widget's own page size; larger is ignored
MAX_PAGES = 250               # ceiling: 2500 reviews/product. The largest real
                              # product seen is 845 (CRBN 7604310704280), so this
                              # guards a paging bug without truncating live data.
SLEEP_MIN, SLEEP_MAX = 0.35, 1.0

# paddle_products.source -> shopify_catalog brand slug. Only these three brands
# are on Judge.me; JOOLA is Bazaarvoice and Selkirk is Okendo.
SOURCE_TO_SLUG: dict[str, str] = {
    "paddletek_shopify": "paddletek",
    "crbn_shopify": "crbn",
    "sixzero_shopify": "six-zero",
}

_ARIA_RATING_RE = re.compile(r"([1-5])\s*star", re.IGNORECASE)
_LOCATION_TRIM_RE = re.compile(r"^[\s(]+|[\s)]+$")
# Judge.me transparency badges. These mean the reviewer got something of value
# for writing, which is what is_incentivized is meant to capture.
_INCENTIVE_BADGES = ("earned_for_future_purchase", "incentiv", "free_product")
# This one means the merchant migrated the review in from a previous review
# platform. It is PROVENANCE, not duplication: 3,811 CRBN/Paddletek rows carry it
# and only 171 of them actually duplicate a row from another source. It must NOT
# drive `is_syndicated`, which means "this row repeats a review we already hold
# from a canonical source" and is what `?exclude_syndicated=true` filters on —
# conflating the two hid 3,640 unique brand-site reviews from reporting.
# The badge is preserved in `context_values.judgeme_badges` instead, and
# `is_syndicated` is set centrally by
# `paddle_store.flag_cross_source_duplicates` once every source is in hand.
_PROVIDER_IMPORT_BADGE = "collected_from_another_provider"


class RatingParseError(RuntimeError):
    """Raised when a `.jdgm-rev` node yields no rating in 1..5.

    Deliberately fatal for the product being scraped: see module docstring.
    """


@dataclass
class ProductContext:
    """Everything a review needs from the already-staged product row."""

    source_product_id: str
    brand: str
    brand_id: str | None
    canonical_name: str | None
    shop: str


@dataclass
class ProductResult:
    """Outcome of scraping one product."""

    source_product_id: str
    source: str                       # the *catalog* source, e.g. crbn_shopify
    brand: str
    status: str                       # ok | text_unavailable | empty | not_listed | error
    review_count: int = 0             # platform-reported total
    avg_rating: float | None = None
    reviews: list[ReviewRecord] = field(default_factory=list)
    rating_parse_failures: int = 0
    error: str | None = None


# ============================================================================ #
# Fragment parsing                                                               #
# ============================================================================ #

def _text(el: Tag | None) -> str:
    return el.get_text(" ", strip=True) if el is not None else ""


def _attr(node: Tag, name: str) -> str | None:
    val = node.get(name)
    if isinstance(val, list):
        val = " ".join(val)
    val = (val or "").strip() if isinstance(val, str) else None
    return val or None


def parse_rating(node: Tag) -> int:
    """Return the 1..5 star rating for one `.jdgm-rev` node.

    Three carriers are tried because they are independently liable to change;
    all three vanishing means the markup moved and the run must stop.
    """
    holder = node.select_one(".jdgm-rev__rating")

    if holder is not None:
        score = _attr(holder, "data-score")
        if score and score.strip().isdigit() and 1 <= int(score) <= 5:
            return int(score)

        aria = _attr(holder, "aria-label") or ""
        m = _ARIA_RATING_RE.search(aria)
        if m:
            return int(m.group(1))

        lit = len(holder.select("span.jdgm-star.jdgm--on"))
        if 1 <= lit <= 5:
            return lit

    raise RatingParseError(
        f"no rating in 1..5 on review {_attr(node, 'data-review-id')!r}; "
        "Judge.me markup for .jdgm-rev__rating has changed"
    )


def _posted_at(node: Tag) -> str | None:
    ts = node.select_one(".jdgm-rev__timestamp")
    if ts is None:
        return None
    # `datetime` is ISO-8601 Z; `data-content` is a human "YYYY-MM-DD HH:MM UTC".
    return _attr(ts, "datetime") or _attr(ts, "data-content")


def _brand_response(node: Tag) -> str | None:
    """Merchant reply text, if the shop answered this review."""
    reply = node.select_one(".jdgm-rev__reply-content") or node.select_one(".jdgm-rev__reply")
    text = _text(reply)
    return text or None


def _media_urls(node: Tag) -> list[str]:
    urls: list[str] = []
    for a in node.select(".jdgm-rev__pic-link"):
        href = _attr(a, "href") or _attr(a, "data-mfp-src")
        if href:
            urls.append(href)
    for v in node.select(".jdgm-rev__vid-link"):
        href = _attr(v, "href") or _attr(v, "data-mfp-src")
        if href:
            urls.append(href)
    return urls


def _badges(node: Tag) -> list[str]:
    """All transparency badge types on one review.

    Must be select_all, not select_one: Judge.me stacks badges, and the
    incentive badge is consistently the *second* one after
    `review_collected_via_store_invitation`. Reading only the first silently
    reported zero incentivised reviews across the whole corpus.
    """
    out: list[str] = []
    for badge in node.select(".jdgm-rev__transparency-badge"):
        val = _attr(badge, "data-badge-type")
        if val and val not in out:
            out.append(val)
    return out


def _location(node: Tag) -> str | None:
    """`(United States)` -> `United States`; the widget ships the parens."""
    raw = _text(node.select_one(".jdgm-rev__location"))
    cleaned = _LOCATION_TRIM_RE.sub("", raw)
    return cleaned or None


def _int_attr(node: Tag, name: str) -> int:
    raw = _attr(node, name) or "0"
    return int(raw) if raw.lstrip("-").isdigit() else 0


def _external_id(node: Tag, ctx: ProductContext, body: str | None, rating: int,
                 reviewer: str | None) -> str:
    """Prefer Judge.me's own review id; fall back to a *stable* content hash.

    Neither a uuid4 nor an array index may be used here: the upsert is keyed on
    `(source, external_review_id)`, so an unstable id turns every re-run into
    duplicate rows instead of an idempotent update.
    """
    rid = _attr(node, "data-review-id") or _attr(node, "id")
    if rid:
        return rid
    digest = content_hash(body, rating, reviewer)[:16]
    return f"{ctx.shop}:{ctx.source_product_id}:{digest}"


def parse_review(node: Tag, ctx: ProductContext) -> ReviewRecord:
    """Map one `.jdgm-rev` node onto a ReviewRecord. Raises on a bad rating."""
    rating = parse_rating(node)
    body = _text(node.select_one(".jdgm-rev__body")) or None
    title = _text(node.select_one(".jdgm-rev__title")) or None
    # Pass the raw author through: ReviewRecord.finalise() is the single place
    # that normalises "" / "Anonymous" to NULL.
    reviewer = _text(node.select_one(".jdgm-rev__author")) or None
    badges = _badges(node)

    return ReviewRecord(
        source=SOURCE,
        external_review_id=_external_id(node, ctx, body, rating, reviewer),
        source_product_id=ctx.source_product_id,
        brand=ctx.brand,
        brand_id=ctx.brand_id,
        canonical_name=ctx.canonical_name,
        reviewer_name=reviewer,
        reviewer_location=_location(node),
        rating=rating,
        title=title,
        body=body,
        posted_at=_posted_at(node),
        is_verified=_attr(node, "data-verified-buyer") == "true"
        or node.select_one(".jdgm-rev__buyer-badge") is not None,
        is_incentivized=any(k in b for b in badges for k in _INCENTIVE_BADGES),
        # Left False on purpose — see _PROVIDER_IMPORT_BADGE. Cross-source
        # duplication can only be judged with every source loaded.
        is_syndicated=False,
        helpful_count=_int_attr(node, "data-thumb-up-count"),
        unhelpful_count=_int_attr(node, "data-thumb-down-count"),
        brand_response=_brand_response(node),
        media_urls=_media_urls(node),
        language_code=_attr(node, "data-review-language"),
        context_values={"judgeme_badges": badges} if badges else None,
    )


def parse_fragment(html: str, ctx: ProductContext) -> list[ReviewRecord]:
    """Parse every `.jdgm-rev` in a widget fragment.

    Propagates RatingParseError so a markup change fails the run (plan §8).
    """
    soup = BeautifulSoup(html, "lxml")
    return [parse_review(node, ctx) for node in soup.select(".jdgm-rev")]


# ============================================================================ #
# HTTP                                                                           #
# ============================================================================ #

def _shop_of(source: str) -> ShopifyBrand:
    slug = SOURCE_TO_SLUG.get(source)
    if slug is None:
        raise ValueError(f"{source!r} is not a Judge.me brand")
    return BRANDS_BY_SLUG[slug]


async def _sleep_polite() -> None:
    await asyncio.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))


async def fetch_widget_page(
    client: httpx.AsyncClient, brand: ShopifyBrand, product_id: str, page: int
) -> dict[str, Any]:
    """One `reviews_for_widget` call. Returns the decoded JSON body.

    The Referer header is what makes the endpoint serve a shop's data without
    auth; it is the widget's own contract, not an access-control bypass.
    """
    resp = await client.get(
        WIDGET_URL,
        params={
            "url": brand.myshopify,
            "shop_domain": brand.myshopify,
            "platform": "shopify",
            "product_id": product_id,
            "page": page,
            "per_page": PER_PAGE,
        },
        headers={"Referer": f"{brand.storefront}/", "User-Agent": USER_AGENT,
                 "Accept": "application/json"},
    )
    resp.raise_for_status()
    # A wrong/unknown product id answers with HTML, not JSON.
    return resp.json()


def _summary_only_result(
    payload: dict[str, Any], product_row: dict[str, Any], brand_display: str
) -> ProductResult:
    """Judge.me v3 (Six Zero): aggregates are public, review text is not.

    Accepted outcome per plan decision D13. `avg_rating` arrives as a string.
    """
    raw_avg = payload.get("average_rating")
    try:
        avg = float(raw_avg) if raw_avg not in (None, "") else None
    except (TypeError, ValueError):
        avg = None
    return ProductResult(
        source_product_id=str(product_row["source_product_id"]),
        source=product_row["source"],
        brand=brand_display,
        status="text_unavailable",
        review_count=int(payload.get("number_of_reviews") or 0),
        avg_rating=avg if avg else None,
    )


async def scrape_product(
    client: httpx.AsyncClient, product_row: dict[str, Any]
) -> ProductResult:
    """Scrape every review page for one staged product row."""
    brand = _shop_of(product_row["source"])
    product_id = str(product_row["source_product_id"])
    ctx = ProductContext(
        source_product_id=product_id,
        brand=brand.display,
        brand_id=product_row.get("brand_id"),
        canonical_name=product_row.get("canonical_name") or product_row.get("title"),
        shop=brand.myshopify,
    )
    result = ProductResult(
        source_product_id=product_id, source=product_row["source"],
        brand=brand.display, status="ok",
    )

    seen: set[str] = set()
    try:
        for page in range(1, MAX_PAGES + 1):
            payload = await fetch_widget_page(client, brand, product_id, page)

            if "html" not in payload:
                # v3 React widget — summary only, no review bodies anywhere.
                return _summary_only_result(payload, product_row, brand.display)

            result.review_count = int(payload.get("total_count") or 0)
            batch = parse_fragment(payload["html"], ctx)
            if not batch:
                break
            fresh = 0
            for rec in batch:
                if rec.external_review_id not in seen:
                    seen.add(rec.external_review_id)
                    result.reviews.append(rec)
                    fresh += 1
            # Paging past the end re-serves page 1 rather than an empty page, so
            # "no new ids" is the real end-of-list signal, not "no nodes".
            if fresh == 0 or len(result.reviews) >= result.review_count:
                break
            await _sleep_polite()
        else:
            log.warning("judgeme_page_ceiling_hit", product_id=product_id,
                        pages=MAX_PAGES, collected=len(result.reviews))
    except RatingParseError as exc:
        # Loud, not silent: a markup change must not become NULL ratings.
        result.status = "error"
        result.rating_parse_failures += 1
        result.error = str(exc)
        log.error("judgeme_rating_parse_failed", product_id=product_id, error=str(exc))
        return result
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            # Judge.me 404s a product it has never collected a review for. That
            # is a fact about the catalog, not a scrape failure — verified on
            # Paddletek "Phoenix Genesis Carbon" (9120060342526).
            result.status = "not_listed"
            log.info("judgeme_product_not_listed", product_id=product_id)
            return result
        result.status = "error"
        result.error = f"HTTP {exc.response.status_code}"
        log.warning("judgeme_fetch_failed", product_id=product_id, error=result.error)
        return result
    except (httpx.HTTPError, ValueError) as exc:
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        log.warning("judgeme_fetch_failed", product_id=product_id, error=result.error[:200])
        return result

    ratings = [r.rating for r in result.reviews if r.rating]
    result.avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None
    if not result.reviews and result.review_count == 0:
        result.status = "empty"
    log.info("judgeme_product_done", product_id=product_id, brand=brand.display,
             status=result.status, reviews=len(result.reviews),
             total_count=result.review_count)
    return result


async def scrape_products(rows: Iterable[dict[str, Any]]) -> list[ProductResult]:
    """Sequentially scrape staged product rows. One shared HTTP client."""
    results: list[ProductResult] = []
    limits = httpx.Limits(max_connections=4)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, limits=limits,
                                 follow_redirects=True) as client:
        for row in rows:
            results.append(await scrape_product(client, row))
            await _sleep_polite()
    return results
