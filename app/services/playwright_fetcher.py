"""Playwright Tier-2 JS-render fallback.

Used only when `FetchResult.needs_js_render` is true. Browser is launched once
per crawl run and reused across pages to amortise startup cost.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import structlog

from app.services.crawler import FetchResult

log = structlog.get_logger()

# Lazy import — playwright is heavy and we only want to pay startup if used.
_playwright_async = None


async def _ensure_imports():
    global _playwright_async
    if _playwright_async is None:
        from playwright.async_api import async_playwright  # type: ignore

        _playwright_async = async_playwright
    return _playwright_async


@asynccontextmanager
async def browser_context(user_agent: str):
    pw_factory = await _ensure_imports()
    pw = await pw_factory().start()
    try:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=user_agent)
        try:
            yield context
        finally:
            await context.close()
            await browser.close()
    finally:
        await pw.stop()


async def fetch_with_playwright(url: str, context, timeout_sec: int = 25) -> FetchResult:
    page = await context.new_page()
    try:
        try:
            response = await page.goto(url, wait_until="networkidle", timeout=timeout_sec * 1000)
        except Exception as e:
            log.info("playwright_goto_failed", url=url, error=str(e))
            return FetchResult(url=url, final_url=url, status=0, fetcher="playwright", error=str(e))

        # Some pages settle later; one-shot wait for body.
        try:
            await page.wait_for_selector("body", timeout=5_000)
        except Exception:
            pass

        # Optional brief settle for SPAs that render after networkidle.
        await asyncio.sleep(0.5)

        final_url = page.url
        status = response.status if response else 0
        text = await page.content()
        return FetchResult(
            url=url,
            final_url=final_url,
            status=status,
            redirect_chain=[],
            text=text,
            content_type="text/html",
            fetcher="playwright",
        )
    finally:
        await page.close()
