"""Robots.txt + sitemap discovery.

Used at the start of a crawl to:
  - Determine which URLs we're allowed to fetch (robots).
  - Seed the BFS frontier with sitemap URLs in addition to the homepage.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
import structlog
from robotexclusionrulesparser import RobotExclusionRulesParser

log = structlog.get_logger()

# Strip default sitemap-index XML namespaces before iterating elements.
_SM_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


@dataclass
class DiscoveryResult:
    base_url: str
    robots_url: str
    robots_text: str = ""
    robots_parser: RobotExclusionRulesParser | None = None
    sitemap_urls: list[str] = field(default_factory=list)  # sitemap.xml file URLs
    seed_urls: list[str] = field(default_factory=list)     # URLs of actual pages found in sitemaps
    blocked_root: bool = False                             # robots disallows our user-agent at /

    def is_allowed(self, url: str, user_agent: str) -> bool:
        if not self.robots_parser:
            return True
        try:
            return self.robots_parser.is_allowed(user_agent, url)
        except Exception:
            return True


async def discover(
    website_url: str, user_agent: str, client: httpx.AsyncClient
) -> DiscoveryResult:
    parsed = urlparse(website_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    robots_url = urljoin(base, "/robots.txt")

    result = DiscoveryResult(base_url=base, robots_url=robots_url)

    # 1. Fetch robots.txt
    try:
        r = await client.get(robots_url, follow_redirects=True, timeout=10.0)
        if r.status_code == 200 and r.text:
            result.robots_text = r.text
            parser = RobotExclusionRulesParser()
            parser.parse(r.text)
            result.robots_parser = parser
            result.blocked_root = not parser.is_allowed(user_agent, base + "/")
    except httpx.HTTPError as e:
        log.warning("robots_fetch_failed", url=robots_url, error=str(e))

    # 2. Collect sitemap URLs from robots.txt directive + common defaults
    sitemap_urls: list[str] = []
    if result.robots_text:
        for line in result.robots_text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("sitemap:"):
                sm = stripped.split(":", 1)[1].strip()
                if sm:
                    sitemap_urls.append(sm)
    for default in ("/sitemap.xml", "/sitemap_index.xml"):
        candidate = urljoin(base, default)
        if candidate not in sitemap_urls:
            sitemap_urls.append(candidate)

    # 3. Fetch + parse each sitemap (recursively for index files)
    seen_seeds: set[str] = set()
    seen_sitemaps: set[str] = set()
    for sm_url in sitemap_urls:
        await _ingest_sitemap(sm_url, client, result, seen_seeds, seen_sitemaps)

    result.sitemap_urls = sorted(seen_sitemaps)
    result.seed_urls = sorted(seen_seeds)
    return result


async def _ingest_sitemap(
    url: str,
    client: httpx.AsyncClient,
    result: DiscoveryResult,
    seen_seeds: set[str],
    seen_sitemaps: set[str],
    depth: int = 0,
) -> None:
    if url in seen_sitemaps or depth > 3:
        return
    seen_sitemaps.add(url)
    try:
        r = await client.get(url, follow_redirects=True, timeout=15.0)
    except httpx.HTTPError as e:
        log.info("sitemap_fetch_failed", url=url, error=str(e))
        return
    if r.status_code != 200 or not r.content:
        return

    # Try to parse as XML; tolerate plain-text sitemaps too.
    text = r.text
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # Plain-text sitemap fallback: one URL per line.
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("http"):
                seen_seeds.add(line)
        return

    tag = root.tag.lower()
    if tag.endswith("sitemapindex"):
        for sm in root.findall(".//sm:sitemap/sm:loc", _SM_NS) + root.findall(".//sitemap/loc"):
            child = (sm.text or "").strip()
            if child:
                await _ingest_sitemap(child, client, result, seen_seeds, seen_sitemaps, depth + 1)
    elif tag.endswith("urlset"):
        for u in root.findall(".//sm:url/sm:loc", _SM_NS) + root.findall(".//url/loc"):
            link = (u.text or "").strip()
            if link.startswith("http"):
                seen_seeds.add(link)
