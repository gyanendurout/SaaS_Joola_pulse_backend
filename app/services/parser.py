"""HTML parser — extracts every field we persist to the `pages` table.

All extraction is deterministic. No LLM here.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

_WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass
class ParsedPage:
    title: str | None = None
    meta_description: str | None = None
    h1: list[str] = field(default_factory=list)
    h2: list[str] = field(default_factory=list)
    h3: list[str] = field(default_factory=list)
    canonical: str | None = None
    robots_meta: str | None = None
    is_indexable: bool = True
    word_count: int = 0
    text_content: str = ""

    internal_links: list[str] = field(default_factory=list)
    external_links: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    images_missing_alt: int = 0

    schema_types: list[str] = field(default_factory=list)
    schema_raw: list[dict] = field(default_factory=list)
    open_graph: dict[str, str] = field(default_factory=dict)
    hreflang: list[dict] = field(default_factory=list)

    content_hash: str | None = None
    template_hint: str | None = None


def _text(el: Tag | None) -> str:
    return el.get_text(" ", strip=True) if el is not None else ""


def _attr(el: Tag | None, name: str) -> str | None:
    if el is None:
        return None
    val = el.get(name)
    if isinstance(val, list):
        return " ".join(val) if val else None
    return val


def _normalize_link(href: str, base_url: str) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    try:
        absolute = urljoin(base_url, href)
        absolute, _ = urldefrag(absolute)
        if absolute.startswith(("http://", "https://")):
            return absolute
    except Exception:
        return None
    return None


def _is_internal(url: str, base_domain: str) -> bool:
    try:
        return urlparse(url).netloc.endswith(base_domain)
    except Exception:
        return False


def _template_hint(url: str) -> str:
    """Crude template fingerprint based on URL path structure.

    /products/abc123 -> /products/_     |    /blog/foo-bar -> /blog/_
    """
    parts = urlparse(url).path.strip("/").split("/")
    return "/" + "/".join("_" if i % 2 else p for i, p in enumerate(parts))


def parse_html(html: str, fetched_url: str, base_domain: str) -> ParsedPage:
    soup = BeautifulSoup(html, "lxml")
    p = ParsedPage()

    # ----- title / meta -----
    if soup.title and soup.title.string:
        p.title = soup.title.string.strip()
    desc_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    p.meta_description = _attr(desc_tag, "content")

    # ----- headings -----
    p.h1 = [_text(h) for h in soup.find_all("h1") if _text(h)]
    p.h2 = [_text(h) for h in soup.find_all("h2") if _text(h)]
    p.h3 = [_text(h) for h in soup.find_all("h3") if _text(h)]

    # ----- canonical / robots -----
    canonical_tag = soup.find("link", attrs={"rel": re.compile("^canonical$", re.I)})
    p.canonical = _attr(canonical_tag, "href")
    robots_tag = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    p.robots_meta = _attr(robots_tag, "content")
    if p.robots_meta and "noindex" in p.robots_meta.lower():
        p.is_indexable = False

    # ----- main text + word count (strip nav/footer/scripts/styles) -----
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    body = soup.body or soup
    text_content = body.get_text(" ", strip=True) if body else ""
    text_content = re.sub(r"\s+", " ", text_content)
    p.text_content = text_content[:10_000]
    p.word_count = len(_WORD_RE.findall(text_content))

    # ----- links -----
    internal: set[str] = set()
    external: set[str] = set()
    for a in soup.find_all("a", href=True):
        link = _normalize_link(a["href"], fetched_url)
        if not link:
            continue
        (internal if _is_internal(link, base_domain) else external).add(link)
    p.internal_links = sorted(internal)
    p.external_links = sorted(external)

    # ----- images -----
    images: list[str] = []
    missing = 0
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src:
            absolute = _normalize_link(src, fetched_url)
            if absolute:
                images.append(absolute)
        alt = (img.get("alt") or "").strip()
        if not alt:
            missing += 1
    p.image_urls = images
    p.images_missing_alt = missing

    # ----- schema.org JSON-LD -----
    types: set[str] = set()
    raw_schemas: list[dict] = []
    for script in soup.find_all("script", attrs={"type": re.compile("application/ld\\+json", re.I)}):
        text_blob = (script.string or script.get_text() or "").strip()
        if not text_blob:
            continue
        try:
            data = json.loads(text_blob)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_schemas.append(item)
            t = item.get("@type")
            if isinstance(t, str):
                types.add(t)
            elif isinstance(t, list):
                types.update(x for x in t if isinstance(x, str))
    p.schema_types = sorted(types)
    p.schema_raw = raw_schemas

    # ----- open graph -----
    og: dict[str, str] = {}
    for tag in soup.find_all("meta", attrs={"property": re.compile("^og:", re.I)}):
        prop = (tag.get("property") or "").lower()
        content = tag.get("content")
        if prop and content:
            og[prop] = content
    p.open_graph = og

    # ----- hreflang -----
    hreflangs: list[dict] = []
    for tag in soup.find_all("link", attrs={"rel": re.compile("alternate", re.I), "hreflang": True}):
        hreflangs.append({"hreflang": tag.get("hreflang"), "href": tag.get("href")})
    p.hreflang = hreflangs

    # ----- dedup helpers -----
    normalized = re.sub(r"\s+", " ", text_content).strip().lower()
    if normalized:
        p.content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    p.template_hint = _template_hint(fetched_url)

    return p
