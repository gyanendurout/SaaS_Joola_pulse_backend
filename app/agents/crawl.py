"""Agent A — Crawl & Page Extraction.

Pipeline (per run):
  1. Resolve canonical domain, fetch robots.txt, fetch sitemaps.
  2. Seed BFS frontier with homepage + sitemap URLs (deduped).
  3. Concurrently fetch (httpx); fall back to Playwright when JS render needed.
  4. Parse each successful HTML response.
  5. Classify page type (rule-based).
  6. Persist parsed fields to `pages`; gzip raw HTML to `storage/`.
  7. Discover new internal links and queue them (respecting max_pages and robots).
  8. Live-update `runs.pages_crawled` so the UI progress bar moves.

Tier 3 (Apify) is wired but only runs when `apify_enabled=true` AND both Tier 1
and Tier 2 returned non-OK. Off by default.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any
from urllib.parse import urldefrag, urlparse
from uuid import UUID

import structlog
import tldextract

from app.config import get_settings
from app.db import service_client
from app.services import storage as storage_svc
from app.services.crawler import FetchResult, fetch, make_client
from app.services.discovery import discover
from app.services.page_classifier import classify
from app.services.parser import parse_html

log = structlog.get_logger()


def _base_domain(url: str) -> str:
    e = tldextract.extract(url)
    return ".".join(p for p in [e.domain, e.suffix] if p)


def _is_html_ish(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return "html" in ct or ct.startswith("application/xhtml")


async def run_crawl(run_id: UUID) -> dict:
    settings = get_settings()
    db = service_client()

    run_res = db.table("runs").select("*").eq("id", str(run_id)).single().execute()
    if not run_res.data:
        raise RuntimeError(f"Run {run_id} not found")
    run = run_res.data

    website_url: str = run["website_url"]
    max_pages: int = int(run["max_pages"])
    apify_enabled: bool = bool(run["apify_enabled"])
    base_domain = _base_domain(website_url)

    fetcher_counts = {"httpx": 0, "playwright": 0, "apify": 0}
    pages_failed = 0
    pages_crawled = 0
    seen: set[str] = set()
    queue: deque[str] = deque()

    pw_context = None  # lazy-init Playwright context the first time we need it

    async with make_client(settings.user_agent, settings.crawl_request_timeout_sec) as client:
        # --- 1) Discover ---
        disc = await discover(website_url, settings.user_agent, client)
        if disc.blocked_root:
            log.warning("robots_blocks_root", url=website_url)

        # --- 2) Seed frontier ---
        seeds = [website_url] + disc.seed_urls
        for s in seeds:
            url, _ = urldefrag(s)
            if url in seen:
                continue
            seen.add(url)
            queue.append(url)

        sem = asyncio.Semaphore(settings.crawl_concurrency)

        async def process(url: str) -> None:
            nonlocal pages_crawled, pages_failed, pw_context

            if not disc.is_allowed(url, settings.user_agent):
                log.info("robots_disallow", url=url)
                return

            # --- Tier 1: httpx ---
            try:
                async with sem:
                    result = await fetch(url, client)
            except Exception as e:
                log.exception("fetch_unhandled", url=url)
                result = FetchResult(url=url, final_url=url, status=0, error=str(e))

            # --- Tier 2: Playwright fallback (lazy import + lazy context) ---
            if result.needs_js_render:
                from app.services.playwright_fetcher import (
                    browser_context as pw_browser_context,
                    fetch_with_playwright,
                )

                if pw_context is None:
                    # Acquire the async generator and store the active context.
                    # Caller is responsible for closing via `pw_close()` at end.
                    cm = pw_browser_context(settings.user_agent)
                    pw_context = {"cm": cm, "ctx": await cm.__aenter__()}
                try:
                    pw_result = await fetch_with_playwright(url, pw_context["ctx"])
                    if pw_result.ok and len(pw_result.text) > len(result.text or ""):
                        result = pw_result
                except Exception as e:
                    log.warning("playwright_failed", url=url, error=str(e))

            # --- Tier 3: Apify (off by default) ---
            if apify_enabled and not result.ok:
                # Stub — will implement in week 2 if joola.com proves it's needed.
                log.info("apify_skipped_stub", url=url)

            # --- Persist ---
            if not result.ok or not _is_html_ish(result.content_type):
                pages_failed += 1
                _persist_failed_page(db, str(run_id), result)
                return

            try:
                parsed = parse_html(result.text, result.final_url, base_domain)
                page_type = classify(result.final_url, parsed)
                page_id = _persist_parsed_page(db, str(run_id), result, parsed, page_type)
                if page_id:
                    storage_svc.write_html(str(run_id), page_id, result.text)
                fetcher_counts[result.fetcher] = fetcher_counts.get(result.fetcher, 0) + 1
                pages_crawled += 1
                _bump_run_counters(db, str(run_id), pages_crawled, pages_failed)
            except Exception:
                log.exception("parse_or_persist_failed", url=url)
                pages_failed += 1
                return

            # --- Discover more internal links ---
            for link in parsed.internal_links:
                link, _ = urldefrag(link)
                if link in seen:
                    continue
                if not _same_site(link, base_domain):
                    continue
                if (pages_crawled + len(queue)) >= max_pages:
                    break
                seen.add(link)
                queue.append(link)

        # --- 3) Drain the frontier in waves ---
        try:
            while queue and pages_crawled < max_pages:
                # Take up to `concurrency` URLs per wave
                wave: list[str] = []
                for _ in range(settings.crawl_concurrency):
                    if not queue:
                        break
                    wave.append(queue.popleft())
                if not wave:
                    break
                # Stop if this run was cancelled
                if _is_cancelled(db, str(run_id)):
                    log.info("crawl_cancelled", run_id=str(run_id))
                    break
                await asyncio.gather(*(process(u) for u in wave), return_exceptions=False)
        finally:
            # Tear down Playwright context if we opened one
            if pw_context is not None:
                try:
                    await pw_context["cm"].__aexit__(None, None, None)
                except Exception:
                    log.exception("playwright_close_failed")

    return {
        "run_id": str(run_id),
        "pages_crawled": pages_crawled,
        "pages_failed": pages_failed,
        "fetcher_breakdown": fetcher_counts,
        "sitemap_found": bool(disc.seed_urls),
        "robots_blocked": disc.blocked_root,
    }


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def _same_site(url: str, base_domain: str) -> bool:
    try:
        return urlparse(url).netloc.endswith(base_domain)
    except Exception:
        return False


def _is_cancelled(db, run_id: str) -> bool:
    try:
        res = db.table("runs").select("status").eq("id", run_id).single().execute()
        return (res.data or {}).get("status") == "cancelled"
    except Exception:
        return False


def _bump_run_counters(db, run_id: str, crawled: int, failed: int) -> None:
    db.table("runs").update(
        {"pages_crawled": crawled, "pages_failed": failed}
    ).eq("id", run_id).execute()


def _persist_failed_page(db, run_id: str, result: FetchResult) -> None:
    db.table("pages").upsert(
        {
            "run_id": run_id,
            "url": result.url,
            "final_url": result.final_url,
            "http_status": result.status,
            "redirect_chain": result.redirect_chain,
            "fetcher": result.fetcher,
        },
        on_conflict="run_id,url",
    ).execute()


def _persist_parsed_page(
    db, run_id: str, result: FetchResult, parsed, page_type: str
) -> str | None:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "url": result.url,
        "final_url": result.final_url,
        "http_status": result.status,
        "redirect_chain": result.redirect_chain,
        "fetcher": result.fetcher,
        # parsed
        "title": parsed.title,
        "meta_description": parsed.meta_description,
        "h1": parsed.h1,
        "h2": parsed.h2,
        "h3": parsed.h3,
        "canonical": parsed.canonical,
        "robots_meta": parsed.robots_meta,
        "is_indexable": parsed.is_indexable,
        "word_count": parsed.word_count,
        "text_content": parsed.text_content,
        "internal_links": parsed.internal_links,
        "external_links": parsed.external_links,
        "image_urls": parsed.image_urls,
        "images_missing_alt": parsed.images_missing_alt,
        "schema_types": parsed.schema_types,
        "schema_raw": parsed.schema_raw,
        "open_graph": parsed.open_graph,
        "hreflang": parsed.hreflang,
        "page_type": page_type,
        "page_type_source": "rule",
        "content_hash": parsed.content_hash,
        "template_hint": parsed.template_hint,
    }
    res = (
        db.table("pages")
        .upsert(payload, on_conflict="run_id,url")
        .execute()
    )
    if res.data:
        return res.data[0]["id"]
    return None
