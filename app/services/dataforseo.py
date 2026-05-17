"""DataForSEO client.

Endpoints used:
  - Keyword Ideas: /v3/dataforseo_labs/google/keyword_ideas/live
  - SERP Organic:  /v3/serp/google/organic/live/regular

Auth: HTTP Basic with login + password.
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.config import get_settings

log = structlog.get_logger()

BASE_URL = "https://api.dataforseo.com"

# Country code -> DataForSEO location_code (most-used subset).
LOCATION_CODES = {
    "US": 2840, "GB": 2826, "CA": 2124, "AU": 2036, "IN": 2356,
    "DE": 2276, "FR": 2250, "ES": 2724, "IT": 2380, "NL": 2528,
    "JP": 2392, "BR": 2076, "MX": 2484,
}

# Language code -> DataForSEO language_code (most-used subset).
LANGUAGE_CODES = {
    "en": "en", "es": "es", "fr": "fr", "de": "de", "it": "it",
    "pt": "pt", "ja": "ja", "nl": "nl", "ru": "ru", "zh": "zh-CN",
}


def _auth() -> tuple[str, str]:
    s = get_settings()
    return (s.dataforseo_login, s.dataforseo_password)


def _location_code(market: str) -> int:
    return LOCATION_CODES.get(market.upper(), 2840)


def _language_code(language: str) -> str:
    return LANGUAGE_CODES.get(language.lower(), "en")


async def keyword_ideas(
    seeds: list[str],
    market: str = "US",
    language: str = "en",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return a flat list of keyword-idea items for the given seeds.

    Each item is the raw DataForSEO 'item' dict — caller normalizes.
    """
    if not seeds:
        return []
    payload = [
        {
            "keywords": seeds[:200],
            "location_code": _location_code(market),
            "language_code": _language_code(language),
            "limit": limit,
            "include_seed_keyword": True,
            "include_serp_info": False,
        }
    ]
    url = f"{BASE_URL}/v3/dataforseo_labs/google/keyword_ideas/live"
    async with httpx.AsyncClient(timeout=60.0, auth=_auth()) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
    tasks = data.get("tasks") or []
    items: list[dict[str, Any]] = []
    for task in tasks:
        for result in task.get("result") or []:
            for item in result.get("items") or []:
                items.append(item)
    return items


async def ranked_keywords(
    domain: str,
    market: str = "US",
    language: str = "en",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Keywords the domain currently ranks for (top positions), ordered by search volume desc."""
    payload = [
        {
            "target": domain,
            "location_code": _location_code(market),
            "language_code": _language_code(language),
            "limit": limit,
            "order_by": ["keyword_data.keyword_info.search_volume,desc"],
        }
    ]
    url = f"{BASE_URL}/v3/dataforseo_labs/google/ranked_keywords/live"
    async with httpx.AsyncClient(timeout=60.0, auth=_auth()) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
    items: list[dict[str, Any]] = []
    for task in data.get("tasks") or []:
        for result in task.get("result") or []:
            for item in result.get("items") or []:
                items.append(item)
    return items


async def competitors_domain(
    domain: str,
    market: str = "US",
    language: str = "en",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Organic competitor domains sharing the most keyword overlap."""
    payload = [
        {
            "target": domain,
            "location_code": _location_code(market),
            "language_code": _language_code(language),
            "limit": limit,
        }
    ]
    url = f"{BASE_URL}/v3/dataforseo_labs/google/competitors_domain/live"
    async with httpx.AsyncClient(timeout=60.0, auth=_auth()) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
    items: list[dict[str, Any]] = []
    for task in data.get("tasks") or []:
        for result in task.get("result") or []:
            for item in result.get("items") or []:
                items.append(item)
    return items


async def backlinks_overview(domain: str) -> dict[str, Any]:
    """Summary backlink metrics for a domain."""
    payload = [{"target": domain, "include_subdomains": True}]
    url = f"{BASE_URL}/v3/backlinks/domain_pages_summary/live"
    async with httpx.AsyncClient(timeout=60.0, auth=_auth()) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
    for task in data.get("tasks") or []:
        for result in task.get("result") or []:
            if result:
                return result
    return {}


async def serp_organic(
    keyword: str,
    market: str = "US",
    language: str = "en",
    depth: int = 10,
) -> dict[str, Any]:
    """Return a normalized SERP result for one keyword."""
    payload = [
        {
            "keyword": keyword,
            "location_code": _location_code(market),
            "language_code": _language_code(language),
            "depth": depth,
        }
    ]
    url = f"{BASE_URL}/v3/serp/google/organic/live/regular"
    async with httpx.AsyncClient(timeout=60.0, auth=_auth()) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()

    organic: list[dict[str, Any]] = []
    paa: list[str] = []
    related: list[str] = []
    for task in data.get("tasks") or []:
        for result in task.get("result") or []:
            for item in result.get("items") or []:
                t = item.get("type")
                if t == "organic":
                    organic.append({
                        "rank": item.get("rank_absolute"),
                        "title": item.get("title"),
                        "url": item.get("url"),
                        "domain": item.get("domain"),
                        "description": item.get("description"),
                    })
                elif t == "people_also_ask":
                    for q in item.get("items") or []:
                        if "title" in q:
                            paa.append(q["title"])
                elif t == "related_searches":
                    for s in item.get("items") or []:
                        if isinstance(s, str):
                            related.append(s)
    return {
        "keyword": keyword,
        "organic": organic[:depth],
        "people_also_ask": paa[:10],
        "related_searches": related[:10],
    }
