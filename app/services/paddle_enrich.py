"""AI enrichment for paddle reviews (Stage 9 of PADDLE_REVIEWS_PLAN.md §5).

Batches unenriched reviews through the cheap model and fills the AI columns:
`sentiment_label`, `sentiment_score`, `topics`, `is_crisis`, `is_opportunity`,
`complaint_category`, `mentioned_competitors`.

Prompt shape and label vocabulary follow `scripts/enrich_social.py` so paddle
reviews stay comparable with the TikTok/Reddit/X enrichment already in the DB.

Deliberately separate from `source_sentiment`: Yotpo ships its own sentiment
value, which we keep as an independent cross-check rather than overwriting
(§2.5). Never write a source's sentiment into `sentiment_label`.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

from app.config import get_settings
from app.services.llm import chat_json
from app.services.paddle_store import TRACKED_BRANDS

log = structlog.get_logger()

BATCH_SIZE = 20
MAX_BODY_CHARS = 900          # reviews run long; truncation keeps batches cheap

SENTIMENT_LABELS = {"positive", "negative", "neutral"}

TOPIC_VOCAB = (
    "power", "control", "spin", "feel", "durability", "grip", "weight",
    "balance", "noise", "price_value", "shipping", "customer_service",
    "quality_defect", "warranty", "comparison", "recommendation", "general",
)

COMPLAINT_CATEGORIES = (
    "delamination", "core_crush", "edge_guard", "grip_wear", "paint_chipping",
    "broken_handle", "dead_spot", "shipping_damage", "wrong_item",
    "customer_service", "price", "durability_other", "none",
)

CRISIS_SIGNALS = (
    "delamination", "cracked", "broke", "snapped", "core crush", "dead spot",
    "injury", "unsafe", "banned", "illegal", "counterfeit", "fake",
    "refund refused", "warranty denied", "lawsuit", "recall",
)


def _system_prompt() -> str:
    return f"""You analyze customer reviews of pickleball paddles.
These are consumer product reviews collected from brand storefronts and retailers.
The brands in scope are: {', '.join(TRACKED_BRANDS)}.

Crisis signals (set is_crisis=true when the review describes one): {', '.join(CRISIS_SIGNALS)}.

For EACH item in the input list, output a JSON object with EXACTLY these keys:
  i: the integer "i" copied verbatim from the input item this result is for
  sentiment_label: "positive"|"negative"|"neutral"
  sentiment_score: float in [-1.0, 1.0]
  topics: array of strings (max 4, chosen from: {', '.join(TOPIC_VOCAB)})
  is_crisis: boolean (true for a product-safety issue, structural failure,
             delamination/core failure, counterfeit claim, or denied warranty)
  is_opportunity: boolean — reserve this for a real commercial signal: the
             reviewer bought another one, says they will buy again, or switched TO
             this brand from a named competitor. Liking the paddle is NOT an
             opportunity on its own, and neither is a generic recommendation.
  complaint_category: one of {', '.join(COMPLAINT_CATEGORIES)} ("none" when not a complaint)
  mentioned_competitors: array of brand names actually named in the text (empty array if none)

A star rating is supplied for context. Trust the TEXT over the rating when they
disagree, and say so via sentiment_label.

Do NOT default complaint_category to "none" on a negative review that names a
concrete cause. "not worth the money" is price. A return, refund, warranty or
support dispute is customer_service. Reserve "none" for reviews that raise no
complaint at all.

Return one result per input item, each carrying its own "i". Never merge, skip or
reorder items — "i" is what pairs a result back to its review.

Respond ONLY as JSON: {{"results": [ ...one object per input item... ]}}"""


def _clean_label(value: Any, allowed: Any, default: str) -> str:
    text = str(value or "").strip().lower().replace(" ", "_")
    return text if text in allowed else default


def _clamp_score(value: Any) -> float:
    try:
        return max(-1.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _payload_item(row: dict[str, Any], index: int) -> dict[str, Any]:
    body = (row.get("body") or "")[:MAX_BODY_CHARS]
    return {
        "i": index,
        "brand": row.get("brand") or "unknown",
        "product": row.get("canonical_name") or row.get("source_product_id") or "",
        "rating": row.get("rating"),
        "title": (row.get("title") or "")[:200],
        "text": body,
    }


def normalise_result(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce one model result into DB-safe column values."""
    topics_raw = raw.get("topics") or []
    topics = [
        t for t in (str(x).strip().lower().replace(" ", "_") for x in topics_raw)
        if t in TOPIC_VOCAB
    ][:4]

    comps_raw = raw.get("mentioned_competitors") or []
    known = {b.lower(): b for b in TRACKED_BRANDS}
    comps = []
    for c in comps_raw:
        hit = known.get(str(c).strip().lower())
        if hit and hit not in comps:
            comps.append(hit)

    return {
        "sentiment_label": _clean_label(raw.get("sentiment_label"), SENTIMENT_LABELS, "neutral"),
        "sentiment_score": _clamp_score(raw.get("sentiment_score")),
        "topics": topics,
        "is_crisis": bool(raw.get("is_crisis")),
        "is_opportunity": bool(raw.get("is_opportunity")),
        "complaint_category": _clean_label(
            raw.get("complaint_category"), set(COMPLAINT_CATEGORIES), "none"
        ),
        "mentioned_competitors": comps,
    }


async def enrich_batch(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enrich one batch. Returns `{id, ...ai columns}` patches, order-aligned.

    A batch whose response is unusable is skipped rather than half-applied — the
    rows simply stay unenriched and get picked up on the next run.
    """
    if not rows:
        return []
    payload = [_payload_item(r, i) for i, r in enumerate(rows)]
    try:
        resp = await chat_json(
            system=_system_prompt(),
            user=json.dumps(payload, ensure_ascii=False),
            model=get_settings().openai_model_cheap,
            temperature=0,
        )
    except Exception as e:
        log.warning("enrich_batch_failed", size=len(rows), error=str(e)[:200])
        return []

    results = resp.get("results") or []
    if not isinstance(results, list):
        log.warning("enrich_batch_shape", size=len(rows))
        return []

    # Pair results back to reviews by the echoed "i", NEVER by list position.
    # Positional pairing looked fine on uniform test batches but silently
    # mislabelled real ones: the model drops or merges items on heterogeneous
    # input, which shifts every row after the gap. That wrote "positive" onto a
    # 1-star "Damaged paddle" before this was caught.
    by_index: dict[int, dict[str, Any]] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        raw_i = item.get("i")
        try:
            idx = int(raw_i)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(rows):
            by_index.setdefault(idx, item)

    if len(by_index) < len(rows):
        log.warning("enrich_batch_incomplete", sent=len(rows), usable=len(by_index))

    patches: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        item = by_index.get(i)
        if item is None:
            continue                      # left NULL; a re-run retries this row
        patch = normalise_result(item)
        # Carry the upsert conflict target so results can be written in batches.
        patch["source"] = row.get("source")
        patch["external_review_id"] = row.get("external_review_id")
        patches.append(patch)
    return patches


def fetch_unenriched(db: Any, limit: int = 500) -> list[dict[str, Any]]:
    """Reviews that still need enrichment (sentiment_label IS NULL).

    `source` and `external_review_id` come along because they are the upsert
    conflict target used by `apply_patches` to write results back in batches.
    """
    try:
        res = (
            db.table("paddle_reviews")
            .select(
                "id, source, external_review_id, brand, canonical_name, "
                "source_product_id, rating, title, body"
            )
            .is_("sentiment_label", "null")
            .not_.is_("body", "null")
            .order("posted_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [r for r in (res.data or []) if (r.get("body") or "").strip()]
    except Exception as e:
        log.warning("fetch_unenriched_failed", error=str(e)[:200])
        return []


def apply_patches(db: Any, patches: list[dict[str, Any]], chunk: int = 200) -> int:
    """Write AI columns back in batches. Returns rows written.

    Upserts on `(source, external_review_id)` rather than issuing one UPDATE per
    review: at 22k rows the row-by-row shape means 22k HTTP round-trips and
    dominates the whole job. Every row already exists, so the conflict branch
    always fires and only the AI columns in the payload are touched — the scrape
    data is never clobbered.
    """
    rows = [p for p in patches if p.get("source") and p.get("external_review_id")]
    written = 0
    for i in range(0, len(rows), chunk):
        batch = rows[i:i + chunk]
        try:
            db.table("paddle_reviews").upsert(
                batch, on_conflict="source,external_review_id"
            ).execute()
            written += len(batch)
        except Exception as e:
            log.warning("enrich_apply_failed", size=len(batch), error=str(e)[:200])
    return written


async def enrich_reviews(db: Any, *, limit: int = 500, sleep: float = 0.4) -> int:
    """Enrich up to `limit` reviews. Returns the number of rows updated."""
    rows = fetch_unenriched(db, limit=limit)
    if not rows:
        log.info("enrich_nothing_to_do")
        return 0

    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        patches = await enrich_batch(batch)
        total += apply_patches(db, patches)
        log.info(
            "enrich_progress",
            done=min(i + BATCH_SIZE, len(rows)), of=len(rows), updated=total,
        )
        await asyncio.sleep(sleep)
    return total
