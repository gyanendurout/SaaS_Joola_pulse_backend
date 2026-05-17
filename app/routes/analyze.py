"""Analyze routes: start a single-URL analysis + SSE event stream + dashboard."""
from __future__ import annotations

import asyncio
import ipaddress
import json
from urllib.parse import urlparse
from uuid import UUID

import tldextract
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, HttpUrl

from app.agents.single_url import run_single_url_analysis
from app.config import get_settings
from app.db import service_client
from app.services import event_bus

router = APIRouter(prefix="/api/analyze", tags=["analyze"])

# Hard-coded seed-keyword defaults per canonical domain.
# These are used ONLY when no user-curated list exists yet (first run for a URL).
# joola.com is a pickleball + table tennis brand; we default to pickleball
# per product owner directive — never tennis / table tennis.
DOMAIN_SEED_DEFAULTS: dict[str, list[str]] = {
    "joola.com": [
        "joola pickleball",
        "joola pickleball paddle",
        "pickleball paddle",
        "pickleball paddles",
        "graphite pickleball paddle",
        "carbon fiber pickleball paddle",
        "best pickleball paddle",
        "pickleball paddle reviews",
        "pickleball paddles for beginners",
        "pickleball paddles for advanced players",
        "joola perseus",
        "joola hyperion",
        "joola ben johns",
        "ben johns pickleball paddle",
        "pickleball racket",
    ],
}


class AnalyzeRequest(BaseModel):
    url: HttpUrl
    market: str | None = None
    language: str | None = None
    force: bool = False  # bypass cache and re-run


def _is_internal(host: str) -> bool:
    if host in {"localhost", ""}:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


@router.post("")
async def start_analysis(req: AnalyzeRequest, background: BackgroundTasks):
    settings = get_settings()
    parsed = urlparse(str(req.url))
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(400, "URL must be http(s)")
    if _is_internal(parsed.hostname or ""):
        raise HTTPException(400, "Internal / loopback hosts are not allowed")

    e = tldextract.extract(parsed.netloc)
    canonical_domain = ".".join(p for p in [e.domain, e.suffix] if p)
    url_str = str(req.url)
    market = (req.market or settings.default_market).upper()
    language = (req.language or settings.default_language).lower()

    db = service_client()

    # ---- Cache check: if a completed run exists for same URL + market + language ----
    if not req.force:
        cache_res = (
            db.table("runs")
            .select("id,created_at,status,market,language")
            .eq("website_url", url_str)
            .eq("market", market)
            .eq("language", language)
            .eq("crawl_mode", "single_url")
            .eq("status", "done")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if cache_res.data:
            cached = cache_res.data[0]
            return {
                "id": cached["id"],
                "url": url_str,
                "market": market,
                "language": language,
                "status": "done",
                "cached": True,
                "cached_at": cached["created_at"],
                "events_url": f"/api/analyze/{cached['id']}/events",
            }

    # ---- Find previous completed run for gap analysis ----
    prev_res = (
        db.table("runs")
        .select("id, seed_keywords")
        .eq("website_url", url_str)
        .eq("market", market)
        .eq("language", language)
        .eq("crawl_mode", "single_url")
        .eq("status", "done")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    previous_run_id = prev_res.data[0]["id"] if prev_res.data else None
    previous_seeds: list[str] | None = None
    if prev_res.data and prev_res.data[0].get("seed_keywords"):
        previous_seeds = prev_res.data[0]["seed_keywords"]

    # ---- Resolve initial seed_keywords for this run ----
    # Priority: previous run's curated list > domain default > None (let LLM decide).
    initial_seeds: list[str] | None = None
    if previous_seeds:
        initial_seeds = previous_seeds
    elif canonical_domain in DOMAIN_SEED_DEFAULTS:
        initial_seeds = DOMAIN_SEED_DEFAULTS[canonical_domain]

    payload: dict = {
        "website_url": url_str,
        "canonical_domain": canonical_domain,
        "market": market,
        "language": language,
        "crawl_mode": "single_url",
        "max_pages": 1,
        "status": "pending",
    }
    if previous_run_id:
        payload["previous_run_id"] = previous_run_id
    if initial_seeds:
        payload["seed_keywords"] = initial_seeds

    res = db.table("runs").insert(payload).execute()
    if not res.data:
        raise HTTPException(500, "Failed to create analysis")
    run = res.data[0]
    run_id = run["id"]

    background.add_task(run_single_url_analysis, UUID(run_id))

    return {
        "id": run_id,
        "url": run["website_url"],
        "market": run["market"],
        "language": run["language"],
        "status": run["status"],
        "cached": False,
        "previous_run_id": previous_run_id,
        "events_url": f"/api/analyze/{run_id}/events",
    }


class SeedKeywordsRequest(BaseModel):
    seed_keywords: list[str]


@router.put("/{run_id}/seed-keywords")
async def update_seed_keywords(run_id: UUID, body: SeedKeywordsRequest):
    """Save a user-curated list of seed keywords on the run."""
    db = service_client()
    cleaned = [k.strip() for k in body.seed_keywords if k and k.strip()]
    # de-dupe preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for k in cleaned:
        kl = k.lower()
        if kl in seen:
            continue
        seen.add(kl)
        deduped.append(k)
    res = (
        db.table("runs")
        .update({"seed_keywords": deduped})
        .eq("id", str(run_id))
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Run not found")
    return {"seed_keywords": deduped}


@router.get("/{run_id}/dashboard")
async def get_dashboard(run_id: UUID):
    """Full dashboard data — all tables joined for a single run."""
    db = service_client()
    rid = str(run_id)

    run_res = db.table("runs").select("*").eq("id", rid).single().execute()
    if not run_res.data:
        raise HTTPException(404, "Analysis not found")
    run = run_res.data

    def _safe(fn):
        try:
            return fn()
        except Exception:
            return []

    pages = _safe(lambda: db.table("pages").select("*").eq("run_id", rid).execute().data or [])
    issues = _safe(lambda: db.table("issues").select("*").eq("run_id", rid).order("severity").execute().data or [])
    entities = _safe(lambda: db.table("entities").select("*").eq("run_id", rid).execute().data or [])
    keywords = _safe(lambda: (
        db.table("keywords")
        .select("*")
        .eq("run_id", rid)
        .order("search_volume", desc=True)
        .limit(200)
        .execute().data or []
    ))
    serp_results = _safe(lambda: db.table("serp_results").select("*").eq("run_id", rid).execute().data or [])
    backlinks_rows = _safe(lambda: db.table("backlinks_summary").select("*").eq("run_id", rid).limit(1).execute().data or [])
    competitors = _safe(lambda: (
        db.table("competitor_domains")
        .select("*")
        .eq("run_id", rid)
        .order("intersections", desc=True)
        .execute().data or []
    ))
    ranked_kws = _safe(lambda: (
        db.table("domain_ranked_keywords")
        .select("*")
        .eq("run_id", rid)
        .order("position")
        .limit(100)
        .execute().data or []
    ))
    gap_rows = _safe(lambda: db.table("gap_analyses").select("*").eq("run_id", rid).limit(1).execute().data or [])

    # Deserialize recommendations from runs.recommendations (JSON string)
    recs = None
    raw_recs = run.get("recommendations")
    if raw_recs:
        try:
            recs = json.loads(raw_recs) if isinstance(raw_recs, str) else raw_recs
        except Exception:
            recs = None

    # Deserialize serp_results.organic (stored as JSON string)
    for sr in serp_results:
        if isinstance(sr.get("organic"), str):
            try:
                sr["organic"] = json.loads(sr["organic"])
            except Exception:
                sr["organic"] = []

    # Deserialize gap_analyses JSON columns
    gap = None
    if gap_rows:
        gap = gap_rows[0]
        for col in ["new_issues", "fixed_issues", "new_ranked_keywords", "lost_ranked_keywords",
                    "rank_improvements", "rank_declines"]:
            if isinstance(gap.get(col), str):
                try:
                    gap[col] = json.loads(gap[col])
                except Exception:
                    gap[col] = []

    return {
        "run": run,
        "page": pages[0] if pages else None,
        "issues": issues,
        "entities": entities,
        "keywords": keywords,
        "serp_results": serp_results,
        "backlinks": backlinks_rows[0] if backlinks_rows else None,
        "competitors": competitors,
        "ranked_keywords": ranked_kws,
        "recommendations": recs,
        "gap_analysis": gap,
    }


@router.get("/{run_id}/events")
async def stream_events(run_id: UUID, since: int = Query(0, ge=0)):
    """Server-Sent Events stream of pipeline progress."""
    rid = str(run_id)

    async def event_generator():
        try:
            async for evt in event_bus.subscribe(rid, since=since):
                yield f"event: {evt['type']}\n"
                yield f"id: {evt['seq']}\n"
                yield f"data: {json.dumps(evt, default=str)}\n\n"
        except asyncio.CancelledError:
            return

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(event_generator(), media_type="text/event-stream", headers=headers)


@router.get("/{run_id}")
async def get_analysis(run_id: UUID):
    db = service_client()
    run_res = db.table("runs").select("*").eq("id", str(run_id)).single().execute()
    if not run_res.data:
        raise HTTPException(404, "Analysis not found")
    return {
        "run": run_res.data,
        "events": event_bus.get_log(str(run_id)),
    }


@router.get("")
async def list_analyses(
    limit: int = Query(20, ge=1, le=100),
    url: str | None = Query(None),
):
    """Recent single-URL analyses. Pass ?url= to filter by exact URL."""
    db = service_client()
    q = (
        db.table("runs")
        .select("id,website_url,canonical_domain,market,language,status,created_at,finished_at,previous_run_id")
        .eq("crawl_mode", "single_url")
        .order("created_at", desc=True)
        .limit(limit)
    )
    if url:
        q = q.eq("website_url", url)
    res = q.execute()
    return {"items": res.data or []}
