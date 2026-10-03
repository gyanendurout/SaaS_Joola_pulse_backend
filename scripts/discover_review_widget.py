"""Review-widget discovery tool.

Encodes decision D2 from PADDLE_REVIEWS_PLAN.md: a static HTML fetch produces
false negatives on client-rendered review widgets, so every new source must be
verified with a rendered network capture before concluding "no reviews".

Two passes per target:
  PASS 1 (static, httpx)      platform detection, robots.txt, Shopify catalog
  PASS 2 (rendered, Playwright)  network capture -> review API endpoints

Usage:
    python scripts/discover_review_widget.py                 # all targets, both passes
    python scripts/discover_review_widget.py --static-only   # skip Playwright
    python scripts/discover_review_widget.py --target crbn
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30.0
INTER_REQUEST_SLEEP = 0.6  # plan risk row: 0.35-1.0s, zero blocks hit during research

# Candidate domains per brand: the first that answers 200 wins.
TARGETS: dict[str, list[str]] = {
    "paddletek": ["https://www.paddletek.com", "https://paddletek.com"],
    "crbn": ["https://crbnpickleball.com", "https://www.crbnpickleball.com"],
    "sixzero": [
        "https://sixzeropickleball.com",
        "https://www.sixzero.com",
        "https://sixzeropaddles.com",
        "https://www.sixzeropickleball.com.au",
    ],
    "pickleballcentral": ["https://pickleballcentral.com", "https://www.pickleballcentral.com"],
    "dicks": ["https://www.dickssportinggoods.com"],
}

# Collection paths to probe for a Shopify JSON catalog.
CATALOG_PATHS = [
    "/collections/pickleball-paddles/products.json?limit=250",
    "/collections/paddles/products.json?limit=250",
    "/collections/all/products.json?limit=250",
    "/products.json?limit=250",
]

# Review-platform fingerprints. Order matters only for reporting.
PLATFORM_MARKERS: dict[str, list[str]] = {
    "Bazaarvoice": ["apps.bazaarvoice.com", "bazaarvoice", "BV_WB_FAMILY", "bvapi", "Bv-Bfd-Token"],
    "Okendo": ["okendo", "api.okendo.io", "okendoProduct", "oke-"],
    "Yotpo": ["yotpo", "staticw2.yotpo.com", "yotpo-widget"],
    "Judge.me": ["judge.me", "jdgm-", "judgeme"],
    "Loox": ["loox.io", "MetafieldLooxRating", "looxReviews"],
    "Stamped.io": ["stamped.io", "stampedio", "stamped-reviews"],
    "Reviews.io": ["reviews.io", "widget.reviews.io"],
    "PowerReviews": ["powerreviews", "ui.powerreviews.com", "pwr-"],
    "Trustpilot": ["trustpilot", "widget.trustpilot.com"],
    "Junip": ["junip", "api.junip.co"],
    "Fera": ["fera.ai", "fera-"],
    "Rivyo": ["rivyo", "thimatic"],
    "Shopify Product Reviews": ["spr-reviews", "shopify-product-reviews"],
    "Bazaarvoice(SEO/syndicated)": ["bvseo", "bv-seo"],
}

# Hosts worth flagging when seen in a rendered network capture.
API_HOST_HINTS = [
    "bazaarvoice",
    "okendo",
    "yotpo",
    "judge.me",
    "judgeme",
    "loox",
    "stamped",
    "reviews.io",
    "powerreviews",
    "junip",
    "fera",
    "trustpilot",
]


@dataclass
class Finding:
    name: str
    base_url: str | None = None
    status: int | None = None
    platform_guess: str = "unknown"
    static_markers: dict[str, int] = field(default_factory=dict)
    is_shopify: bool = False
    catalog_path: str | None = None
    catalog_count: int | None = None
    paddle_count: int | None = None
    sample_product: dict[str, Any] | None = None
    robots_disallows_reviews: list[str] = field(default_factory=list)
    robots_note: str = ""
    bot_protection: str = ""
    rendered_api_calls: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _detect_platforms(text: str) -> dict[str, int]:
    """Count platform fingerprints in a blob of HTML/JS."""
    hits: dict[str, int] = {}
    low = text.lower()
    for platform, markers in PLATFORM_MARKERS.items():
        n = sum(low.count(m.lower()) for m in markers)
        if n:
            hits[platform] = n
    return hits


def _sniff_bot_protection(resp: httpx.Response) -> str:
    server = resp.headers.get("server", "").lower()
    body = (resp.text[:4000] if resp.text else "").lower()
    signals = []
    if "cloudflare" in server or "cf-ray" in resp.headers:
        signals.append("Cloudflare")
    if "akamai" in server or "akamai" in resp.headers.get("x-cache", "").lower():
        signals.append("Akamai")
    if resp.headers.get("x-akamai-transformed"):
        signals.append("Akamai(transformed)")
    if "perimeterx" in body or "_px" in body:
        signals.append("PerimeterX")
    if "datadome" in body or "datadome" in resp.headers.get("set-cookie", "").lower():
        signals.append("DataDome")
    if resp.status_code in (403, 405, 429) or "access denied" in body or "are you a robot" in body:
        signals.append(f"challenge(status={resp.status_code})")
    return ", ".join(signals)


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    try:
        r = await client.get(url)
        await asyncio.sleep(INTER_REQUEST_SLEEP)
        return r
    except Exception as exc:  # noqa: BLE001 - diagnostic tool, report and continue
        print(f"      ! {type(exc).__name__}: {str(exc)[:90]}")
        return None


async def _resolve_base(client: httpx.AsyncClient, candidates: list[str]) -> tuple[str | None, int | None, httpx.Response | None]:
    for base in candidates:
        r = await _get(client, base + "/")
        if r is not None and r.status_code < 400:
            return base, r.status_code, r
        if r is not None:
            print(f"      {base} -> {r.status_code}")
    return None, (r.status_code if r is not None else None), r


async def _check_robots(client: httpx.AsyncClient, base: str, f: Finding) -> None:
    r = await _get(client, base + "/robots.txt")
    if r is None or r.status_code >= 400:
        f.robots_note = f"robots.txt unavailable (status={getattr(r, 'status_code', 'err')})"
        return
    lines = r.text.splitlines()
    star_block, disallows = False, []
    for raw in lines:
        line = raw.strip()
        low = line.lower()
        if low.startswith("user-agent:"):
            star_block = low.split(":", 1)[1].strip() == "*"
        elif star_block and low.startswith("disallow:"):
            path = line.split(":", 1)[1].strip()
            if path:
                disallows.append(path)
    review_rules = [
        d for d in disallows
        if re.search(r"review|/r/|rating|product", d, re.I)
    ]
    f.robots_disallows_reviews = review_rules[:12]
    f.robots_note = f"{len(lines)} lines, {len(disallows)} Disallow rules for UA:*"


async def _check_catalog(client: httpx.AsyncClient, base: str, f: Finding) -> None:
    """Probe every candidate path and keep the richest one.

    An empty collection still returns HTTP 200 + valid JSON, so returning on the
    first parseable response reports "0 products" for a store that has hundreds.
    """
    best: tuple[str, list[dict[str, Any]]] | None = None
    for path in CATALOG_PATHS:
        r = await _get(client, base + path)
        if r is None or r.status_code >= 400:
            continue
        if "json" not in r.headers.get("content-type", ""):
            continue
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            continue
        products = data.get("products")
        if not isinstance(products, list):
            continue
        f.is_shopify = True
        if best is None or len(products) > len(best[1]):
            best = (path, products)
        if len(products) >= 250:  # full page, no point probing broader paths
            break

    if best is None:
        return
    f.catalog_path, products = best
    f.catalog_count = len(products)
    paddles = [p for p in products if re.search(r"paddle", str(p.get("title", "")), re.I)]
    f.paddle_count = len(paddles)
    pick = (paddles or products)[0] if (paddles or products) else None
    if pick:
        f.sample_product = {
            "id": pick.get("id"),
            "handle": pick.get("handle"),
            "title": (pick.get("title") or "")[:70],
        }


async def _check_product_page(client: httpx.AsyncClient, base: str, f: Finding) -> None:
    """Static pass on a product page - expected to under-report (that is the point)."""
    url = None
    if f.is_shopify and f.sample_product and f.sample_product.get("handle"):
        url = f"{base}/products/{f.sample_product['handle']}"
    if url is None:
        url = base + "/"
    r = await _get(client, url)
    if r is None:
        f.errors.append(f"product page fetch failed: {url}")
        return
    f.status = r.status_code
    f.bot_protection = _sniff_bot_protection(r)
    hits = _detect_platforms(r.text or "")
    f.static_markers = dict(sorted(hits.items(), key=lambda kv: -kv[1]))
    if hits:
        f.platform_guess = max(hits, key=lambda k: hits[k])


async def static_pass(name: str, candidates: list[str]) -> Finding:
    f = Finding(name=name)
    print(f"\n--- {name} " + "-" * (60 - len(name)))
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    async with httpx.AsyncClient(
        headers=headers, timeout=REQUEST_TIMEOUT, follow_redirects=True
    ) as client:
        base, status, resp = await _resolve_base(client, candidates)
        if base is None:
            f.errors.append("no candidate domain returned < 400")
            if resp is not None:
                f.status = resp.status_code
                f.bot_protection = _sniff_bot_protection(resp)
            print(f"      UNREACHABLE (last status={f.status}, protection={f.bot_protection or 'none detected'})")
            return f
        f.base_url = base
        f.status = status
        print(f"      base = {base}  ({status})")
        await _check_robots(client, base, f)
        print(f"      robots: {f.robots_note}")
        if f.robots_disallows_reviews:
            print(f"      robots review-ish Disallow: {f.robots_disallows_reviews}")
        await _check_catalog(client, base, f)
        if f.is_shopify:
            print(f"      Shopify catalog: {f.catalog_count} products "
                  f"({f.paddle_count} titled 'paddle') via {f.catalog_path}")
        else:
            print("      no Shopify products.json (non-Shopify or blocked)")
        await _check_product_page(client, base, f)
        print(f"      static platform markers: {f.static_markers or 'NONE (expect false negative)'}")
        if f.bot_protection:
            print(f"      bot protection: {f.bot_protection}")
    return f


async def rendered_pass(f: Finding) -> None:
    """Playwright network capture - the only reliable detector (plan D2)."""
    if not f.base_url:
        return
    from playwright.async_api import async_playwright

    target = f.base_url + "/"
    if f.is_shopify and f.sample_product and f.sample_product.get("handle"):
        target = f"{f.base_url}/products/{f.sample_product['handle']}"

    seen: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, locale="en-US")
        page = await ctx.new_page()

        def on_request(req: Any) -> None:
            host = urlparse(req.url).netloc.lower()
            if any(h in host for h in API_HOST_HINTS) or re.search(r"review", req.url, re.I):
                entry = f"{req.method} {req.url[:150]}"
                if entry not in seen:
                    seen.append(entry)

        page.on("request", on_request)
        try:
            await page.goto(target, wait_until="networkidle", timeout=45000)
            # Reviews often lazy-load below the fold.
            await page.mouse.wheel(0, 6000)
            await page.wait_for_timeout(3500)
            await page.mouse.wheel(0, 6000)
            await page.wait_for_timeout(2500)
            html = await page.content()
            rendered_hits = _detect_platforms(html)
            if rendered_hits:
                merged = dict(f.static_markers)
                for k, v in rendered_hits.items():
                    merged[k] = max(merged.get(k, 0), v)
                f.static_markers = dict(sorted(merged.items(), key=lambda kv: -kv[1]))
                f.platform_guess = max(rendered_hits, key=lambda k: rendered_hits[k])
        except Exception as exc:  # noqa: BLE001
            f.errors.append(f"rendered pass: {type(exc).__name__}: {str(exc)[:110]}")
        finally:
            await ctx.close()
            await browser.close()

    f.rendered_api_calls = seen[:25]
    print(f"\n  [rendered] {f.name} -> {target[:95]}")
    print(f"      platform now: {f.platform_guess}  markers={f.static_markers or '{}'}")
    if seen:
        print(f"      review-related network calls ({len(seen)}):")
        for s in seen[:12]:
            print(f"        {s}")
    else:
        print("      no review-related network calls captured")
    for e in f.errors:
        print(f"      ! {e}")


def report(findings: list[Finding]) -> None:
    print("\n" + "=" * 78)
    print("DISCOVERY SUMMARY")
    print("=" * 78)
    hdr = f"{'target':20s} {'platform':16s} {'shopify':8s} {'catalog':8s} {'blocked':10s}"
    print(hdr)
    print("-" * 78)
    for f in findings:
        print(
            f"{f.name:20s} {f.platform_guess[:16]:16s} "
            f"{('yes' if f.is_shopify else 'no'):8s} "
            f"{(str(f.catalog_count) if f.catalog_count is not None else '-'):8s} "
            f"{(f.bot_protection[:10] or '-'):10s}"
        )
    out = {
        f.name: {
            "base_url": f.base_url,
            "platform_guess": f.platform_guess,
            "markers": f.static_markers,
            "is_shopify": f.is_shopify,
            "catalog_path": f.catalog_path,
            "catalog_count": f.catalog_count,
            "paddle_count": f.paddle_count,
            "sample_product": f.sample_product,
            "robots_note": f.robots_note,
            "robots_disallows_reviews": f.robots_disallows_reviews,
            "bot_protection": f.bot_protection,
            "rendered_api_calls": f.rendered_api_calls,
            "errors": f.errors,
        }
        for f in findings
    }
    path = "scripts/_discovery_results.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    print(f"\nfull results -> {path}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--static-only", action="store_true", help="skip the Playwright pass")
    ap.add_argument("--target", action="append", help="limit to named target(s)")
    args = ap.parse_args()

    targets = TARGETS
    if args.target:
        targets = {k: v for k, v in TARGETS.items() if k in args.target}
        if not targets:
            print(f"no such target; known: {list(TARGETS)}")
            return 2

    print("=" * 78)
    print("PASS 1 - STATIC (httpx): platform detection, robots, catalog")
    print("=" * 78)
    findings = [await static_pass(name, cands) for name, cands in targets.items()]

    if not args.static_only:
        print("\n" + "=" * 78)
        print("PASS 2 - RENDERED (Playwright): network capture")
        print("=" * 78)
        for f in findings:
            await rendered_pass(f)

    report(findings)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
