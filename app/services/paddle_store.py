"""Shared normalisation + persistence layer for paddle review sourcing.

Every source module (bazaarvoice, okendo, judgeme, yotpo) produces
`ProductRecord` / `ReviewRecord` values and hands them here. This module owns:

  * brand resolution against the existing `brands` table
  * the content hash used for cross-source syndication detection
  * a JSONL staging cache on disk, so a scrape is never lost when the DB is
    not yet migrated
  * idempotent Supabase upserts keyed on the unique constraints from
    migration 011 (`(source, source_product_id)` and `(source, external_review_id)`)

PII rule (PADDLE_REVIEWS_PLAN.md §8): public display names only. Never store
emails or order ids. `_scrub_custom_fields` enforces that for sources that ship
merchant-configurable field bags.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from app.config import WRITABLE_ROOT

log = structlog.get_logger()

STAGING_DIR = WRITABLE_ROOT / "storage" / "paddle_staging"

# Brand slug -> canonical display name used in paddle_products.brand
BRAND_SLUGS: dict[str, str] = {
    "joola": "JOOLA",
    "selkirk": "Selkirk",
    "paddletek": "Paddletek",
    "crbn": "CRBN",
    "six-zero": "Six Zero",
}

# Competitor names scanned for in review bodies during enrichment.
TRACKED_BRANDS: list[str] = [
    "JOOLA", "Selkirk", "Paddletek", "CRBN", "Six Zero", "Engage",
    "Onix", "Franklin", "Head", "Wilson", "Gamma", "Proton", "Vatic",
]

_PII_KEY_RE = re.compile(
    r"(e[-_ ]?mail|phone|mobile|tel|order[-_ ]?(id|no|number)|address|zip|postal|last[-_ ]?name"
    # Demographic attributes about the person, as opposed to attributes of their
    # play. Pickleball Central's Yotpo widget collects an age band whose values
    # include "Under 18", so this bag can carry data about minors. Review
    # intelligence does not need it, and the scope agreed in
    # PADDLE_REVIEWS_PLAN.md is username + text + rating, so it is dropped by
    # default. `Skill Level`, `Frequency` and `Previous Experience` describe how
    # someone plays rather than who they are, and are kept.
    r"|\bage\b|birth|\bdob\b|gender|ethnic|income)",
    re.IGNORECASE,
)


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


# ============================================================================ #
# Records                                                                        #
# ============================================================================ #

@dataclass
class ProductRecord:
    source: str
    source_product_id: str
    brand: str = "other"
    brand_id: str | None = None
    retailer: str | None = None
    family_id: str | None = None
    canonical_name: str | None = None
    title: str | None = None
    handle: str | None = None
    product_url: str | None = None
    image_url: str | None = None
    price: float | None = None
    currency: str = "USD"
    review_count: int = 0
    avg_rating: float | None = None
    rating_distribution: dict[str, Any] | None = None
    gtin: list[str] = field(default_factory=list)
    is_paddle: bool = True
    is_active: bool = True

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["last_seen_at"] = utcnow_iso()
        return row


@dataclass
class ReviewRecord:
    source: str
    external_review_id: str
    source_product_id: str | None = None
    brand: str | None = None
    brand_id: str | None = None
    retailer: str | None = None
    family_id: str | None = None
    canonical_name: str | None = None
    reviewer_name: str | None = None        # nullable by design (§8)
    reviewer_location: str | None = None
    rating: int | None = None
    title: str | None = None
    body: str | None = None
    pros: str | None = None
    cons: str | None = None
    secondary_ratings: dict[str, Any] | None = None
    context_values: dict[str, Any] | None = None
    posted_at: str | None = None
    is_verified: bool | None = None
    is_recommended: bool | None = None
    is_incentivized: bool | None = None
    is_syndicated: bool = False
    helpful_count: int = 0
    unhelpful_count: int = 0
    brand_response: str | None = None
    brand_response_at: str | None = None
    media_urls: list[str] = field(default_factory=list)
    language_code: str | None = None
    source_sentiment: str | None = None
    content_hash: str | None = None

    def finalise(self) -> ReviewRecord:
        """Fill derived fields. Call once before staging/upsert."""
        if not self.content_hash:
            self.content_hash = content_hash(self.body, self.rating, self.reviewer_name)
        if self.context_values:
            self.context_values = _scrub_custom_fields(self.context_values)
        if isinstance(self.reviewer_name, str):
            name = self.reviewer_name.strip()
            # Judge.me emits "" and "Anonymous"; normalise both to NULL.
            self.reviewer_name = None if name.lower() in ("", "anonymous") else name
        return self

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["scraped_at"] = utcnow_iso()
        return row


def content_hash(body: str | None, rating: int | None, reviewer: str | None) -> str:
    """Stable hash for cross-source syndication detection.

    `(source, external_review_id)` cannot catch a retailer republishing a
    brand-site review under a different id — this can (§8).
    """
    norm_body = re.sub(r"\s+", " ", (body or "")).strip().lower()
    norm_name = (reviewer or "").strip().lower()
    payload = f"{norm_name}|{rating if rating is not None else ''}|{norm_body}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scrub_custom_fields(bag: dict[str, Any]) -> dict[str, Any]:
    """Drop any merchant-configured field whose key looks like PII."""
    return {k: v for k, v in bag.items() if not _PII_KEY_RE.search(str(k))}


# ============================================================================ #
# Brand resolution                                                               #
# ============================================================================ #

_brand_cache: dict[str, str] | None = None


def brand_id_map(db: Any) -> dict[str, str]:
    """slug -> brands.id, cached per process."""
    global _brand_cache
    if _brand_cache is not None:
        return _brand_cache
    try:
        res = db.table("brands").select("id, slug").execute()
        _brand_cache = {r["slug"]: r["id"] for r in (res.data or [])}
    except Exception as e:                                   # pragma: no cover
        log.warning("brand_map_failed", error=str(e))
        _brand_cache = {}
    return _brand_cache


def detect_brand(text: str | None) -> tuple[str, str | None]:
    """Best-effort (display_name, slug) from a product title.

    Used for retailer feeds where the brand is not a structural field.
    """
    hay = (text or "").lower()
    for slug, display in BRAND_SLUGS.items():
        needle = display.lower()
        if needle in hay or slug.replace("-", " ") in hay:
            return display, slug
    return "other", None


# ============================================================================ #
# Staging cache                                                                  #
# ============================================================================ #

def _staging_file(name: str) -> Path:
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    return STAGING_DIR / f"{name}.jsonl"


def write_staged(name: str, rows: Iterable[dict[str, Any]]) -> int:
    """Overwrite a staging file with `rows`. Returns the row count written."""
    path = _staging_file(name)
    n = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            n += 1
    log.info("staged", file=path.name, rows=n)
    return n


def append_staged(name: str, rows: Iterable[dict[str, Any]]) -> int:
    path = _staging_file(name)
    n = 0
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            n += 1
    return n


def read_staged(name: str) -> list[dict[str, Any]]:
    path = _staging_file(name)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def list_staged() -> list[str]:
    if not STAGING_DIR.exists():
        return []
    return sorted(p.stem for p in STAGING_DIR.glob("*.jsonl"))


# ============================================================================ #
# Supabase persistence                                                           #
# ============================================================================ #

# PostgREST caps every response at 1000 rows on this project, and it does so
# SILENTLY — `.limit(20000)` and `.range(0, 19999)` both return exactly 1000.
# Any aggregate built from a single select is therefore wrong once a table passes
# 1000 rows, which it long since has. Always page through with `fetch_all_rows`.
POSTGREST_PAGE_SIZE = 1000
MAX_PAGES = 500                    # 500k rows: a backstop, not an expected limit


def fetch_all_rows(
    db: Any,
    table: str,
    columns: str,
    *,
    modifier: Any = None,
    page_size: int = POSTGREST_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Read every row of `table`, paging past the PostgREST response cap.

    `modifier` is an optional callable applied to the query builder to add
    filters, e.g. `modifier=lambda q: q.eq("source", "yotpo")`.
    """
    out: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        start = page * page_size
        query = db.table(table).select(columns)
        if modifier is not None:
            query = modifier(query)
        # Deterministic order is required: without it Postgres may return
        # overlapping or skipped rows across pages, especially while another
        # stage is inserting.
        try:
            batch = (
                query.order("id").range(start, start + page_size - 1).execute().data or []
            )
        except Exception as e:
            # Never return a partial table — a silently short result becomes a
            # wrong aggregate or a missed flag, which is worse than a failure.
            log.error("fetch_all_rows_failed", table=table, page=page, error=str(e)[:200])
            raise
        out.extend(batch)
        if len(batch) < page_size:
            break
    return out


def tables_ready(db: Any) -> bool:
    """True when migration 011 has been applied."""
    try:
        db.table("paddle_reviews").select("id").limit(1).execute()
        return True
    except Exception:
        return False


def _chunks(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def upsert_products(db: Any, rows: list[dict[str, Any]], chunk: int = 200) -> int:
    """Idempotent upsert on (source, source_product_id). Returns rows accepted."""
    written = 0
    for batch in _chunks(rows, chunk):
        try:
            db.table("paddle_products").upsert(
                batch, on_conflict="source,source_product_id"
            ).execute()
            written += len(batch)
        except Exception as e:
            log.warning("product_upsert_failed", size=len(batch), error=str(e)[:300])
            raise
    return written


def upsert_reviews(db: Any, rows: list[dict[str, Any]], chunk: int = 200) -> int:
    """Idempotent upsert on (source, external_review_id). Returns rows accepted."""
    written = 0
    for batch in _chunks(rows, chunk):
        try:
            db.table("paddle_reviews").upsert(
                batch, on_conflict="source,external_review_id"
            ).execute()
            written += len(batch)
        except Exception as e:
            log.warning("review_upsert_failed", size=len(batch), error=str(e)[:300])
            raise
    return written


# Brand-site sources are canonical; a retailer carrying the same review body is
# a syndicated copy. Verified live: 225 bodies are shared between the Judge.me
# brand sites and Pickleball Central's Yotpo feed, and Yotpo's own
# `source_review_id` syndication field is null on every row — so the content
# hash is the ONLY detector for this (PADDLE_REVIEWS_PLAN.md §8).
CANONICAL_SOURCES: frozenset[str] = frozenset({"bazaarvoice", "okendo", "judgeme"})
RETAILER_SOURCES: frozenset[str] = frozenset({"yotpo", "dicks"})


def flag_cross_source_duplicates(rows: list[dict[str, Any]]) -> int:
    """Mark retailer rows whose body already exists on a brand site.

    Mutates `rows` in place, setting `is_syndicated=True` on the retailer copy
    and leaving the brand-site original untouched. Returns the number flagged.

    Deliberately does NOT delete: the duplicate is real data about the retailer's
    shelf, and reporting filters it with `exclude_syndicated` instead.
    """
    canonical_hashes = {
        row.get("content_hash")
        for row in rows
        if row.get("source") in CANONICAL_SOURCES and row.get("content_hash")
    }
    if not canonical_hashes:
        return 0

    flagged = 0
    for row in rows:
        if row.get("source") not in RETAILER_SOURCES:
            continue
        if row.get("content_hash") in canonical_hashes and not row.get("is_syndicated"):
            row["is_syndicated"] = True
            flagged += 1
    return flagged


def link_reviews_to_products(db: Any) -> int:
    """Backfill paddle_reviews.product_id by `source_product_id` match.

    Reviews arrive from a review platform while products arrive from a
    storefront catalog, so the FK cannot be set at insert time for every
    source. Returns the number of review rows linked.

    Issues one UPDATE per distinct product rather than per review: there are a
    few hundred products against tens of thousands of reviews, so the row-by-row
    shape would mean tens of thousands of HTTP round-trips.
    """
    prods = fetch_all_rows(
        db, "paddle_products",
        "id, source_product_id, brand, brand_id, canonical_name, family_id",
    )
    if not prods:
        return 0

    linked = 0
    seen: set[str] = set()
    for prod in prods:
        pid = str(prod.get("source_product_id") or "")
        # A product id can appear under several sources (brand site + retailer);
        # the first row wins so the link stays deterministic across runs.
        if not pid or pid in seen:
            continue
        seen.add(pid)

        patch: dict[str, Any] = {"product_id": prod["id"]}
        for col in ("brand", "brand_id", "canonical_name", "family_id"):
            if prod.get(col):
                patch[col] = prod[col]
        try:
            res = (
                db.table("paddle_reviews").update(patch)
                .eq("source_product_id", pid)
                .is_("product_id", "null")
                .execute()
            )
            linked += len(res.data or [])
        except Exception as e:
            log.warning("link_reviews_failed", source_product_id=pid, error=str(e)[:160])
            continue
    return linked
