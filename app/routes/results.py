"""Result-fetch routes: pages, issues, entities, keywords + exports."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from app.db import service_client

router = APIRouter(prefix="/api/runs/{run_id}", tags=["results"])


@router.get("/pages")
async def list_pages(
    run_id: UUID,
    page_type: str | None = None,
    status: int | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    db = service_client()
    qb = db.table("pages").select("*", count="exact").eq("run_id", str(run_id))
    if page_type:
        qb = qb.eq("page_type", page_type)
    if status is not None:
        qb = qb.eq("http_status", status)
    if q:
        qb = qb.ilike("url", f"%{q}%")
    res = qb.order("created_at").range(offset, offset + limit - 1).execute()
    return {"items": res.data or [], "total": res.count or 0, "limit": limit, "offset": offset}


@router.get("/pages/{page_id}")
async def get_page(run_id: UUID, page_id: UUID):
    db = service_client()
    res = (
        db.table("pages")
        .select("*")
        .eq("run_id", str(run_id))
        .eq("id", str(page_id))
        .single()
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Page not found")
    issues = (
        db.table("issues")
        .select("*")
        .eq("page_id", str(page_id))
        .execute()
    )
    return {"page": res.data, "issues": issues.data or []}


@router.get("/issues")
async def list_issues(
    run_id: UUID,
    severity: str | None = None,
    code: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    db = service_client()
    qb = db.table("issues").select("*", count="exact").eq("run_id", str(run_id))
    if severity:
        qb = qb.eq("severity", severity)
    if code:
        qb = qb.eq("issue_code", code)
    res = qb.range(offset, offset + limit - 1).execute()

    counts_res = (
        db.table("issues").select("severity").eq("run_id", str(run_id)).execute()
    )
    counts: dict[str, int] = {}
    for r in counts_res.data or []:
        counts[r["severity"]] = counts.get(r["severity"], 0) + 1

    return {
        "items": res.data or [],
        "total": res.count or 0,
        "counts_by_severity": counts,
        "limit": limit,
        "offset": offset,
    }


@router.get("/entities")
async def list_entities(run_id: UUID, type: str | None = None):
    db = service_client()
    qb = db.table("entities").select("*").eq("run_id", str(run_id))
    if type:
        qb = qb.eq("entity_type", type)
    res = qb.order("confidence", desc=True).execute()

    counts: dict[str, int] = {}
    for r in res.data or []:
        counts[r["entity_type"]] = counts.get(r["entity_type"], 0) + 1
    return {"items": res.data or [], "counts_by_type": counts}


@router.get("/keywords")
async def list_keywords(
    run_id: UUID,
    intent: str | None = None,
    min_volume: int | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    db = service_client()
    qb = db.table("keywords").select("*", count="exact").eq("run_id", str(run_id))
    if intent:
        qb = qb.eq("intent", intent)
    if min_volume is not None:
        qb = qb.gte("search_volume", min_volume)
    res = qb.order("search_volume", desc=True).range(offset, offset + limit - 1).execute()
    return {"items": res.data or [], "total": res.count or 0, "limit": limit, "offset": offset}
