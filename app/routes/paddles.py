"""Paddle reviews API — sync trigger, SSE progress, products, reviews, analytics.

Mirrors `app/routes/news.py`: a POST kicks off a BackgroundTask keyed on a
`run_id` row, and the client follows progress over SSE from the in-process
event bus.
"""
from __future__ import annotations

import json
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.responses import StreamingResponse

from app.agents.paddle_reviews import STAGES, sync_all_sources
from app.db import service_client
from app.services import event_bus
from app.services.paddle_store import TRACKED_BRANDS, fetch_all_rows

router = APIRouter(prefix="/api/paddles", tags=["paddles"])

def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _cutoff_iso(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


# ============================================================================ #
# Sync endpoints                                                                 #
# ============================================================================ #

@router.post("/sync")
async def trigger_sync(
    background_tasks: BackgroundTasks,
    sources: str | None = Query(
        None, description="Comma-separated source subset, e.g. 'bazaarvoice,okendo'"
    ),
    enrich: bool = Query(True, description="Run the AI enrichment stage"),
) -> dict[str, Any]:
    wanted = [s.strip() for s in (sources or "").split(",") if s.strip()] or None
    run_id = str(uuid.uuid4())
    db = service_client()
    db.table("paddle_review_runs").insert({
        "id": run_id,
        "status": "pending",
        "run_type": "manual",
        "stages_total": len(wanted or STAGES),
        "created_at": _utcnow(),
    }).execute()
    background_tasks.add_task(sync_all_sources, run_id, wanted, enrich)
    return {"run_id": run_id, "sources": wanted or list(STAGES), "enrich": enrich}


@router.get("/sync/{run_id}/events")
async def sync_events(run_id: str) -> StreamingResponse:
    async def _gen():
        async for evt in event_bus.subscribe(run_id):
            yield f"data: {json.dumps(evt)}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sync/latest")
async def latest_sync() -> dict[str, Any]:
    db = service_client()
    res = (
        db.table("paddle_review_runs").select("*")
        .order("created_at", desc=True).limit(1).execute()
    )
    return res.data[0] if res.data else {}


@router.get("/sync/runs")
async def list_runs(limit: int = Query(20, ge=1, le=100)) -> list[dict[str, Any]]:
    db = service_client()
    res = (
        db.table("paddle_review_runs").select("*")
        .order("created_at", desc=True).limit(limit).execute()
    )
    return res.data or []


@router.get("/sync/{run_id}/errors")
async def run_errors(run_id: str, limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
    db = service_client()
    res = (
        db.table("paddle_review_errors").select("*")
        .eq("run_id", run_id).order("created_at", desc=True).limit(limit).execute()
    )
    return res.data or []


# ============================================================================ #
# Products                                                                       #
# ============================================================================ #

@router.get("/products")
async def list_products(
    brand: str | None = Query(None),
    source: str | None = Query(None),
    retailer: str | None = Query(None),
    q: str | None = Query(None),
    has_reviews: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = service_client()
    query = db.table("paddle_products").select("*", count="exact").eq("is_paddle", True)

    if brand:
        query = query.eq("brand", brand)
    if source:
        query = query.eq("source", source)
    if retailer:
        query = query.eq("retailer", retailer)
    if q:
        query = query.ilike("title", f"%{q}%")
    if has_reviews:
        query = query.gt("review_count", 0)

    res = (
        query.order("review_count", desc=True)
        .range(offset, offset + limit - 1).execute()
    )
    return {"total": res.count or 0, "products": res.data or [], "limit": limit, "offset": offset}


# ============================================================================ #
# Reviews                                                                        #
# ============================================================================ #

@router.get("/reviews")
async def list_reviews(
    q: str | None = Query(None, description="Free text over title + body"),
    brand: str | None = Query(None),
    source: str | None = Query(None),
    retailer: str | None = Query(None),
    product_id: str | None = Query(None),
    sentiment: str | None = Query(None),
    complaint_category: str | None = Query(None),
    rating_min: int | None = Query(None, ge=1, le=5),
    rating_max: int | None = Query(None, ge=1, le=5),
    is_crisis: bool | None = Query(None),
    is_opportunity: bool | None = Query(None),
    verified_only: bool = Query(False),
    exclude_syndicated: bool = Query(
        False, description="Drop retailer copies of brand-site reviews"
    ),
    days: int | None = Query(None, ge=1, le=3650),
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = service_client()
    query = db.table("paddle_reviews").select("*", count="exact")

    if brand:
        query = query.eq("brand", brand)
    if source:
        query = query.eq("source", source)
    if retailer:
        query = query.eq("retailer", retailer)
    if product_id:
        query = query.eq("product_id", product_id)
    if sentiment:
        query = query.eq("sentiment_label", sentiment)
    if complaint_category:
        query = query.eq("complaint_category", complaint_category)
    if rating_min is not None:
        query = query.gte("rating", rating_min)
    if rating_max is not None:
        query = query.lte("rating", rating_max)
    if is_crisis is not None:
        query = query.eq("is_crisis", is_crisis)
    if is_opportunity is not None:
        query = query.eq("is_opportunity", is_opportunity)
    if verified_only:
        query = query.eq("is_verified", True)
    if exclude_syndicated:
        query = query.neq("is_syndicated", True)
    if days:
        query = query.gte("posted_at", _cutoff_iso(days))
    if q:
        # PostgREST OR across two text columns.
        query = query.or_(f"title.ilike.%{q}%,body.ilike.%{q}%")

    res = (
        query.order("posted_at", desc=True)
        .range(offset, offset + limit - 1).execute()
    )
    return {"total": res.count or 0, "reviews": res.data or [], "limit": limit, "offset": offset}


# ============================================================================ #
# Analytics                                                                      #
# ============================================================================ #

@router.get("/analytics/summary")
async def analytics_summary(days: int | None = Query(None, ge=1, le=3650)) -> dict[str, Any]:
    """Cross-brand KPI rollup.

    Counts are computed over UNIQUE review rows. Never sum per-product review
    counts: Bazaarvoice rolls variants up into a family and reports the family
    total on every variant, which double-counts badly (PADDLE_REVIEWS_PLAN.md §8).
    """
    db = service_client()
    cutoff = _cutoff_iso(days) if days else None
    # Paged: a single select silently caps at 1000 rows, which would compute
    # these aggregates from ~4% of the table.
    rows = fetch_all_rows(
        db, "paddle_reviews",
        "brand, source, retailer, rating, sentiment_label, is_crisis, "
        "is_opportunity, is_verified, is_syndicated, complaint_category, "
        "content_hash, posted_at",
        modifier=(lambda q: q.gte("posted_at", cutoff)) if cutoff else None,
    )

    by_brand: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"reviews": 0, "rating_sum": 0, "rated": 0, "crisis": 0, "negative": 0}
    )
    ratings = Counter()
    sources = Counter()
    sentiments = Counter()
    complaints = Counter()
    hashes = Counter()
    crisis = opportunity = verified = syndicated = 0
    rating_sum = rated = 0

    for r in rows:
        brand = r.get("brand") or "unknown"
        slot = by_brand[brand]
        slot["reviews"] += 1
        sources[r.get("source") or "unknown"] += 1

        rating = r.get("rating")
        if isinstance(rating, int):
            ratings[rating] += 1
            rating_sum += rating
            rated += 1
            slot["rating_sum"] += rating
            slot["rated"] += 1

        label = r.get("sentiment_label")
        if label:
            sentiments[label] += 1
            if label == "negative":
                slot["negative"] += 1
        if r.get("is_crisis"):
            crisis += 1
            slot["crisis"] += 1
        if r.get("is_opportunity"):
            opportunity += 1
        if r.get("is_verified"):
            verified += 1
        if r.get("is_syndicated"):
            syndicated += 1
        cat = r.get("complaint_category")
        if cat and cat != "none":
            complaints[cat] += 1
        if r.get("content_hash"):
            hashes[r["content_hash"]] += 1

    brands = [
        {
            "brand": b,
            "reviews": v["reviews"],
            "avg_rating": round(v["rating_sum"] / v["rated"], 2) if v["rated"] else None,
            "crisis": v["crisis"],
            "negative": v["negative"],
        }
        for b, v in sorted(by_brand.items(), key=lambda kv: -kv[1]["reviews"])
    ]

    return {
        "window_days": days,
        "reviews_total": len(rows),
        "avg_rating": round(rating_sum / rated, 2) if rated else None,
        "rating_distribution": {str(k): ratings[k] for k in (5, 4, 3, 2, 1)},
        "by_brand": brands,
        "by_source": dict(sources.most_common()),
        "sentiment_mix": dict(sentiments.most_common()),
        "top_complaint_categories": dict(complaints.most_common(10)),
        "crisis_count": crisis,
        "opportunity_count": opportunity,
        "verified_count": verified,
        "syndicated_count": syndicated,
        # Same body+rating+reviewer appearing under two ids: the cross-source
        # syndication signal that a unique-id check cannot catch.
        "duplicate_content_groups": sum(1 for c in hashes.values() if c > 1),
        "tracked_brands": TRACKED_BRANDS,
        "truncated": False,   # fetch_all_rows pages the whole table
    }


@router.get("/analytics/products")
async def analytics_products(
    brand: str | None = Query(None),
    limit: int = Query(25, ge=1, le=100),
) -> dict[str, Any]:
    """Best / worst reviewed paddles by unique-review average."""
    db = service_client()
    rows = fetch_all_rows(
        db, "paddle_reviews", "canonical_name, brand, rating",
        modifier=(lambda q: q.eq("brand", brand)) if brand else None,
    )

    agg: dict[tuple[str, str], list[int]] = defaultdict(list)
    for r in rows:
        name = r.get("canonical_name")
        rating = r.get("rating")
        if name and isinstance(rating, int):
            agg[(r.get("brand") or "unknown", name)].append(rating)

    scored = [
        {
            "brand": b,
            "product": name,
            "reviews": len(vals),
            "avg_rating": round(sum(vals) / len(vals), 2),
        }
        for (b, name), vals in agg.items()
        if len(vals) >= 3          # 1-2 reviews is noise, not a ranking
    ]
    scored.sort(key=lambda x: (-x["avg_rating"], -x["reviews"]))
    return {
        "best": scored[:limit],
        "worst": list(reversed(scored[-limit:])) if scored else [],
        "products_ranked": len(scored),
    }
