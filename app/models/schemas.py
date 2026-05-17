"""Pydantic request/response schemas for the API layer."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, field_validator

CrawlMode = Literal["full_site", "sitemap_only", "selected_urls", "product_category_only"]
RunStatus = Literal["pending", "running", "done", "failed", "cancelled"]
Severity = Literal["critical", "high", "medium", "low", "info"]
EntityType = Literal["product", "category", "service", "brand", "topic", "persona"]
Intent = Literal[
    "informational", "commercial", "transactional", "navigational",
    "comparison", "problem", "local", "brand",
]


# ---------- requests ----------
class CreateRunRequest(BaseModel):
    website_url: HttpUrl
    market: str = "US"
    language: str = "en"
    crawl_mode: CrawlMode = "full_site"
    max_pages: int = Field(default=300, ge=1, le=1000)
    apify_enabled: bool = False

    @field_validator("market")
    @classmethod
    def upper_market(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("language")
    @classmethod
    def lower_lang(cls, v: str) -> str:
        return v.lower().strip()


# ---------- responses ----------
class RunSummary(BaseModel):
    id: UUID
    website_url: str
    canonical_domain: str | None = None
    market: str
    language: str
    crawl_mode: CrawlMode
    max_pages: int
    apify_enabled: bool
    status: RunStatus
    current_agent: str | None = None
    pages_crawled: int
    pages_failed: int
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class PageRecord(BaseModel):
    id: UUID
    run_id: UUID
    url: str
    final_url: str | None = None
    http_status: int | None = None
    fetcher: str
    title: str | None = None
    meta_description: str | None = None
    h1: list[str] = []
    canonical: str | None = None
    is_indexable: bool | None = None
    word_count: int | None = None
    page_type: str | None = None
    schema_types: list[str] = []
    images_missing_alt: int = 0
    created_at: datetime


class IssueRecord(BaseModel):
    id: UUID
    run_id: UUID
    page_id: UUID | None
    issue_code: str
    severity: Severity
    source: str
    details: dict[str, Any] = {}
    recommendation: str | None = None
    created_at: datetime


class EntityRecord(BaseModel):
    id: UUID
    run_id: UUID
    entity_type: EntityType
    name: str
    canonical_name: str
    confidence: float
    attributes: dict[str, Any] = {}
    source_page_ids: list[UUID] = []
    source: str
    created_at: datetime


class KeywordRecord(BaseModel):
    id: UUID
    run_id: UUID
    keyword: str
    market: str
    language: str
    search_volume: int | None = None
    cpc: float | None = None
    competition: float | None = None
    keyword_difficulty: int | None = None
    intent: Intent | None = None
    keyword_type: str | None = None
    seed_entity_id: UUID | None = None
    source: str
    suggested_page_id: UUID | None = None
    suggested_action: str | None = None
    created_at: datetime


# (Paged[T] generic intentionally omitted — Python 3.11 compatible.
#  Route handlers return plain dicts with `items` / `total` keys.)
