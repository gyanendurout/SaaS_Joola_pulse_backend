"""Pipeline orchestrator: A -> B -> C -> D, with run + job state updates."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import structlog

from app.agents.crawl import run_crawl
from app.agents.entities import run_entities
from app.agents.issues import run_issues
from app.agents.keywords import run_keywords
from app.db import service_client

log = structlog.get_logger()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_run(run_id: UUID, **fields) -> None:
    service_client().table("runs").update(fields).eq("id", str(run_id)).execute()


def _create_job(run_id: UUID, job_type: str) -> str:
    res = (
        service_client()
        .table("jobs")
        .insert({"run_id": str(run_id), "job_type": job_type, "status": "queued"})
        .execute()
    )
    return res.data[0]["id"]


def _set_job(job_id: str, **fields) -> None:
    service_client().table("jobs").update(fields).eq("id", job_id).execute()


async def run_full_pipeline(run_id: UUID) -> None:
    """Sequential A -> B -> C -> D. Each step's failure marks the run failed and aborts."""
    _set_run(run_id, status="running", started_at=_now(), current_agent="A")

    pipeline = [
        ("A", "crawl", run_crawl),
        ("B", "detect_issues", run_issues),
        ("C", "extract_entities", run_entities),
        ("D", "fetch_keywords", run_keywords),
    ]

    try:
        for agent, job_type, fn in pipeline:
            _set_run(run_id, current_agent=agent)
            job_id = _create_job(run_id, job_type)
            _set_job(job_id, status="running", started_at=_now())
            try:
                result = await fn(run_id)
                _set_job(job_id, status="done", finished_at=_now(), result=result or {})
            except NotImplementedError as e:
                # Stubs while we build — record but don't fail the run during week 1.
                log.warning("agent_stub", agent=agent, reason=str(e))
                _set_job(job_id, status="done", finished_at=_now(), message=f"stub: {e}")
            except Exception as e:
                log.exception("agent_failed", agent=agent)
                _set_job(job_id, status="failed", finished_at=_now(), error=str(e))
                _set_run(
                    run_id,
                    status="failed",
                    finished_at=_now(),
                    error_message=f"Agent {agent} failed: {e}",
                )
                return

        _set_run(run_id, status="done", finished_at=_now(), current_agent=None)
    except Exception as e:
        log.exception("pipeline_failed")
        _set_run(run_id, status="failed", finished_at=_now(), error_message=str(e))
