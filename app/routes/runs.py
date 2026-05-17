"""Run lifecycle routes: create, list, get, cancel, delete."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse
from uuid import UUID

import tldextract
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from app.db import service_client
from app.models.schemas import CreateRunRequest, RunSummary
from app.orchestrator import run_full_pipeline

router = APIRouter(prefix="/api/runs", tags=["runs"])


def _is_internal_host(hostname: str) -> bool:
    if hostname in {"localhost", ""}:
        return True
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def _resolve_canonical_domain(url: str) -> str:
    parsed = urlparse(url)
    extracted = tldextract.extract(parsed.netloc)
    return ".".join(p for p in [extracted.domain, extracted.suffix] if p)


@router.post("", response_model=RunSummary)
async def create_run(req: CreateRunRequest, background: BackgroundTasks) -> RunSummary:
    parsed = urlparse(str(req.website_url))
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(400, "URL must be http(s)")
    if _is_internal_host(parsed.hostname or ""):
        raise HTTPException(400, "Internal / loopback hosts are not allowed")

    payload = {
        "website_url": str(req.website_url),
        "canonical_domain": _resolve_canonical_domain(str(req.website_url)),
        "market": req.market,
        "language": req.language,
        "crawl_mode": req.crawl_mode,
        "max_pages": req.max_pages,
        "apify_enabled": req.apify_enabled,
        "status": "pending",
    }
    db = service_client()
    res = db.table("runs").insert(payload).execute()
    if not res.data:
        raise HTTPException(500, "Failed to create run")
    row = res.data[0]

    background.add_task(run_full_pipeline, row["id"])
    return RunSummary(**row)


@router.get("", response_model=list[RunSummary])
async def list_runs(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    db = service_client()
    res = (
        db.table("runs")
        .select("*")
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return [RunSummary(**r) for r in (res.data or [])]


@router.get("/{run_id}", response_model=RunSummary)
async def get_run(run_id: UUID):
    db = service_client()
    res = db.table("runs").select("*").eq("id", str(run_id)).single().execute()
    if not res.data:
        raise HTTPException(404, "Run not found")
    return RunSummary(**res.data)


@router.post("/{run_id}/cancel", response_model=RunSummary)
async def cancel_run(run_id: UUID):
    db = service_client()
    res = (
        db.table("runs")
        .update({"status": "cancelled"})
        .eq("id", str(run_id))
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Run not found")
    return RunSummary(**res.data[0])


@router.delete("/{run_id}")
async def delete_run(run_id: UUID):
    db = service_client()
    db.table("runs").delete().eq("id", str(run_id)).execute()
    return {"ok": True}
