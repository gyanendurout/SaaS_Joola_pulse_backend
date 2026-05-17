"""Rule-based page-type classifier.

Inputs: URL, schema types, title, h1, breadcrumbs (best-effort).
Output: one of: home | category | product | blog | article | landing | contact | policy | faq | other

POC: deterministic only. LLM tiebreak is out of scope (per the contract).
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from app.services.parser import ParsedPage

PAGE_TYPES = (
    "home", "category", "product", "blog", "article",
    "landing", "contact", "policy", "faq", "other",
)

# URL pattern hints (case-insensitive)
_URL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^/?$"), "home"),
    (re.compile(r"/(product|products|p)/[^/]+/?$", re.I), "product"),
    (re.compile(r"/(category|categories|collections|collection|c|shop)(/[^/]+)?/?$", re.I), "category"),
    (re.compile(r"/(blog|news|articles|posts)/[^/]+/?$", re.I), "blog"),
    (re.compile(r"/(blog|news|articles|posts)/?$", re.I), "category"),  # blog index
    (re.compile(r"/(article|story|guide)/[^/]+/?$", re.I), "article"),
    (re.compile(r"/contact-?(us)?/?$", re.I), "contact"),
    (re.compile(r"/(privacy|terms|policy|cookies|legal|gdpr|dmca|disclaimer)/?", re.I), "policy"),
    (re.compile(r"/faq[s]?/?$", re.I), "faq"),
]

# Schema-type → page-type mapping (highest signal)
_SCHEMA_MAP = {
    "Product": "product",
    "ProductGroup": "product",
    "BlogPosting": "blog",
    "Article": "article",
    "NewsArticle": "article",
    "TechArticle": "article",
    "FAQPage": "faq",
    "ContactPage": "contact",
    "WebSite": None,           # too generic
    "Organization": None,
    "BreadcrumbList": None,
    "ItemList": "category",
    "CollectionPage": "category",
}


def classify(url: str, parsed: ParsedPage) -> str:
    parsed_url = urlparse(url)
    path = parsed_url.path or "/"

    # 1. Schema.org JSON-LD wins when it's a strong type
    for t in parsed.schema_types:
        mapped = _SCHEMA_MAP.get(t)
        if mapped:
            return mapped

    # 2. Homepage shortcut
    if path in ("", "/"):
        return "home"

    # 3. URL pattern match
    for pattern, page_type in _URL_PATTERNS:
        if pattern.search(path):
            return page_type

    # 4. Title hints (very weak — last resort)
    title_lower = (parsed.title or "").lower()
    if "404" in title_lower or "not found" in title_lower:
        return "other"

    return "other"
