"""Yotpo review reader for Pickleball Central (PADDLE_REVIEWS_PLAN.md §2.5, D12).

This is the highest-fidelity source in the plan: one retailer, five tracked
brands, the same buyer population, and the richest field set of any platform we
found — merchant replies, incentivised-review flags and Yotpo's own sentiment.

The widget API lives on Yotpo's own CDN, so unlike the Pickleball Central
storefront it answers plain `httpx`; Playwright is not needed here at all. The
app key below is public by construction — Yotpo publishes it client-side in the
widget loader URL (`staticw2.yotpo.com/{appKey}/widget.js`).

Two rules this module exists to enforce:

* **`sentiment` -> `source_sentiment`, never `sentiment_label`.** Yotpo ships
  its own sentiment score; it is a free cross-check against our LLM enrichment,
  not a replacement for it. `sentiment_label` stays None for the enrichment
  stage to own.
* **`custom_fields` is merchant-configurable, so it is PII-suspect** (plan §8
  and the org rule against processing customer PII). We flatten it to its
  human-readable field titles *before* handing it to `ReviewRecord.finalise()`,
  because the raw keys are opaque ids (`--105907`) that the PII scrubber in
  `paddle_store` cannot possibly judge.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx
import structlog

from app.services.paddle_store import ReviewRecord

log = structlog.get_logger()

SOURCE = "yotpo"
RETAILER = "Pickleball Central"

# Public client-side app key, read from Pickleball Central's own widget loader.
APP_KEY = "RtSQNxiPUpGJTxtmEzc1UmQEIZOqn1f7oMMhQBHu"
API_BASE = "https://api-cdn.yotpo.com/v1/widget"

REQUEST_TIMEOUT = 30.0
PER_PAGE = 100          # verified: the endpoint honours 100 (plan documented 15)
MAX_PAGES = 40          # ceiling: 4,000 reviews/product, far past any real one
SLEEP_MIN = 0.35        # plan §8 rate-limit mitigation: 0.35–1.0s between calls
SLEEP_MAX = 1.0

UA = "JoolaPaddleBot/1.0 (+internal; respects robots.txt)"


def reviews_url(bc_product_id: str) -> str:
    """Widget endpoint for one BigCommerce product id.

    Yotpo keys the widget on the merchant's own product id (its `domain_key`),
    not on Yotpo's internal `product_id`, which is why the BigCommerce catalog
    is the only input this module needs.
    """
    return f"{API_BASE}/{APP_KEY}/products/{bc_product_id}/reviews.json"


async def _sleep() -> None:
    await asyncio.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))


# ============================================================================ #
# Fetch                                                                          #
# ============================================================================ #

async def fetch_page(
    client: httpx.AsyncClient, bc_product_id: str, page: int
) -> dict[str, Any]:
    """One page of the widget response, unwrapped from its `response` envelope."""
    resp = await client.get(
        reviews_url(bc_product_id), params={"per_page": PER_PAGE, "page": page}
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    return payload.get("response") or {}


async def fetch_product_reviews(
    client: httpx.AsyncClient, bc_product_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every review for one product, plus its `bottomline` aggregate.

    Paginates on `pagination.total` rather than trusting `bottomline`, and
    stops on a short/empty page so a drifting total can never spin the loop.
    Reviews are deduped on the Yotpo review `id` — the same review can reappear
    across pages when new ones land mid-walk.
    """
    collected: dict[str, dict[str, Any]] = {}
    bottomline: dict[str, Any] = {}
    total: int | None = None
    page = 1

    while page <= MAX_PAGES:
        data = await fetch_page(client, bc_product_id, page)
        if page == 1:
            bottomline = data.get("bottomline") or {}
        pagination = data.get("pagination") or {}
        if total is None:
            try:
                total = int(pagination.get("total") or 0)
            except (TypeError, ValueError):
                total = 0

        batch = data.get("reviews") or []
        for raw in batch:
            rid = str(raw.get("id") or "")
            if rid:
                collected.setdefault(rid, raw)

        if not batch or len(collected) >= (total or 0) or len(batch) < PER_PAGE:
            break
        page += 1
        await _sleep()

    log.info(
        "yotpo_product", product=bc_product_id, unique=len(collected),
        reported_total=total, pages=page,
    )
    return list(collected.values()), bottomline


# ============================================================================ #
# Normalisation                                                                  #
# ============================================================================ #

def flatten_custom_fields(bag: Any) -> dict[str, Any]:
    """`{'--105907': {'title': 'Frequency', 'value': '3 x a week'}}` -> `{...}`.

    Flattening is a PII control, not cosmetics: `paddle_store._scrub_custom_fields`
    judges a field by its *key*, and the raw keys here are opaque numeric ids.
    Keyed by the merchant-authored title, the scrubber can actually see (and
    drop) a field a merchant configured as "Email" or "Order Number".
    """
    if not isinstance(bag, dict):
        return {}
    out: dict[str, Any] = {}
    for key, val in bag.items():
        if isinstance(val, dict):
            title = str(val.get("title") or key).strip() or str(key)
            out[title] = val.get("value")
        else:
            out[str(key)] = val
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def media_urls(images_data: Any) -> list[str]:
    """Image URLs from `images_data`, preferring the original over the thumb."""
    if not isinstance(images_data, list):
        return []
    urls: list[str] = []
    for img in images_data:
        if not isinstance(img, dict):
            continue
        url = img.get("original_url") or img.get("image_url") or img.get("thumb_url")
        if url:
            urls.append(str(url))
    return urls


def brand_response(comment: Any) -> tuple[str | None, str | None]:
    """Merchant reply as `(text, created_at)`.

    Yotpo returns `comment` as an object; older payloads have been seen as a
    bare string, so both shapes are accepted rather than raising.
    """
    if isinstance(comment, dict):
        text = comment.get("content") or comment.get("comment")
        posted = comment.get("created_at") or comment.get("updated_at")
        return (str(text) if text else None), (str(posted) if posted else None)
    if isinstance(comment, str) and comment.strip():
        return comment.strip(), None
    return None, None


def _int_or_none(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _source_sentiment(raw: Any) -> str | None:
    """Yotpo's own sentiment, stored verbatim as text.

    Quirk worth knowing: the field is a **float in [-1, 1]** (e.g. 0.98738825),
    not the label the plan's field list implies. `source_sentiment` is a TEXT
    column, so the numeric value is kept lossless as its string form rather than
    bucketed here — bucketing is an analysis decision, not an ingest one.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return repr(float(raw))
    text = str(raw).strip()
    return text or None


def to_review_record(
    raw: dict[str, Any],
    *,
    bc_product_id: str,
    brand: str | None = None,
    brand_id: str | None = None,
    canonical_name: str | None = None,
) -> ReviewRecord:
    """One Yotpo review -> `ReviewRecord`. Caller must still `.finalise()`."""
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    reply, reply_at = brand_response(raw.get("comment"))

    return ReviewRecord(
        source=SOURCE,
        external_review_id=str(raw.get("id") or ""),
        source_product_id=bc_product_id,
        brand=brand,
        brand_id=brand_id,
        retailer=RETAILER,
        canonical_name=canonical_name,
        # Public display name only — never the email or order id (§8).
        reviewer_name=(user.get("display_name") or None),
        rating=_int_or_none(raw.get("score")),
        title=(raw.get("title") or None),
        body=(raw.get("content") or None),
        context_values=flatten_custom_fields(raw.get("custom_fields")) or None,
        posted_at=(raw.get("created_at") or None),
        is_verified=bool(raw.get("verified_buyer")),
        is_incentivized=bool(raw.get("is_incentivized")),
        # A `source_review_id` means the row was imported from an upstream
        # platform, i.e. this retailer copy is syndicated (§8 hazard).
        is_syndicated=bool(raw.get("source_review_id")),
        helpful_count=_int_or_none(raw.get("votes_up")) or 0,
        unhelpful_count=_int_or_none(raw.get("votes_down")) or 0,
        brand_response=reply,
        brand_response_at=reply_at,
        media_urls=media_urls(raw.get("images_data")),
        language_code=(raw.get("language") or None),
        source_sentiment=_source_sentiment(raw.get("sentiment")),
        # sentiment_label is intentionally left unset: it belongs to our own
        # LLM enrichment stage, which must not be pre-empted by the vendor's.
    )


def is_deleted(raw: dict[str, Any]) -> bool:
    """Yotpo keeps soft-deleted reviews in the payload; they must not be stored."""
    return bool(raw.get("deleted"))


# ============================================================================ #
# Sweep                                                                          #
# ============================================================================ #

async def fetch_reviews_for_products(
    products: list[dict[str, Any]],
) -> tuple[list[ReviewRecord], list[dict[str, Any]], list[dict[str, Any]]]:
    """Sweep every product and return `(records, product_stats, errors)`.

    `products` rows need `source_product_id`; `brand`, `brand_id` and
    `canonical_name` are carried onto each review when present, because the
    review payload itself has no brand field — on a retailer feed the brand only
    exists on the product.
    """
    records: list[ReviewRecord] = []
    stats: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    headers = {"User-Agent": UA, "Accept": "application/json"}

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, headers=headers, follow_redirects=True
    ) as client:
        for product in products:
            pid = str(product.get("source_product_id") or "").strip()
            if not pid:
                continue
            try:
                raws, bottomline = await fetch_product_reviews(client, pid)
            except Exception as e:
                errors.append({"source_product_id": pid, "error": str(e)[:300]})
                log.warning("yotpo_product_failed", product=pid, error=str(e)[:200])
                await _sleep()
                continue

            kept = [r for r in raws if not is_deleted(r) and r.get("id")]
            for raw in kept:
                records.append(
                    to_review_record(
                        raw,
                        bc_product_id=pid,
                        brand=product.get("brand"),
                        brand_id=product.get("brand_id"),
                        canonical_name=product.get("canonical_name")
                        or product.get("title"),
                    )
                )
            stats.append(
                {
                    "source": SOURCE,
                    "source_product_id": pid,
                    "review_count": len(kept),
                    "avg_rating": avg_rating(bottomline),
                }
            )
            await _sleep()

    return records, stats, errors


def avg_rating(bottomline: dict[str, Any]) -> float | None:
    try:
        score = float(bottomline.get("average_score"))
    except (TypeError, ValueError):
        return None
    return round(score, 2) if score else None
