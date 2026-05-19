"""Pydantic types for the Content Generation API.

Field names + enum values mirror the TS types in
`frontend/lib/content/types.ts` and the spec §4.2.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ─── Enums ────────────────────────────────────────────────────────────────────

class ContentType(str, Enum):
    BLOG = "blog"
    IG_POST = "ig_post"
    TWITTER_RESPONSE = "twitter_response"


class DraftStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class Tone(str, Enum):
    INFORMATIVE = "informative"
    HYPE = "hype"
    CELEBRATORY = "celebratory"
    DEFENSIVE = "defensive"
    EDUCATIONAL = "educational"
    PROMOTIONAL = "promotional"


class Audience(str, Enum):
    RECREATIONAL = "recreational"
    TOURNAMENT = "tournament"
    COACHES = "coaches"
    PARENTS_JUNIORS = "parents_juniors"
    GENERAL_FANS = "general_fans"
    PRESS_MEDIA = "press_media"


class Length(str, Enum):
    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"


class CtaGoal(str, Enum):
    SHOP = "shop"
    SIGNUP = "signup"
    REPLY = "reply"
    NONE = "none"


class RunStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    RATE_LIMITED = "rate_limited"


# ─── Signal config ────────────────────────────────────────────────────────────

class SignalsConfig(BaseModel):
    """Which signal sources to pull and which specific IDs to favor."""

    seo_keywords: bool = True
    top_posts: bool = True
    news: bool = True
    reddit: bool = False
    loyal_fans: bool = False
    player_roster: bool = True
    # Optional explicit ID lists for fine-grained selection from the UI
    selected_keyword_ids: list[str] = Field(default_factory=list)
    selected_post_ids: list[str] = Field(default_factory=list)
    selected_news_ids: list[str] = Field(default_factory=list)
    selected_reddit_ids: list[str] = Field(default_factory=list)


# ─── Per-signal preview shapes (mirror frontend SignalsPreview sub-types) ─────

class SeoSignal(BaseModel):
    keyword: str
    search_volume: int | None = None
    position: float | None = None
    is_gap: bool = False
    difficulty: float | None = None


class TopPostSignal(BaseModel):
    post_id: str
    content_theme: str | None = None
    engagement_rate: float | None = None
    caption_first_line: str | None = None
    thumbnail_url: str | None = None


class NewsSignal(BaseModel):
    id: str
    title: str
    ai_summary: str | None = None
    why_it_matters: str | None = None
    players_mentioned: list[str] = Field(default_factory=list)
    suggested_action: str | None = None
    sentiment: str | None = None
    is_joola_mention: bool = False


class RedditSignal(BaseModel):
    id: str
    title: str
    subreddit: str | None = None
    topics: list[str] = Field(default_factory=list)
    sentiment: str | None = None
    is_crisis: bool = False
    excerpt: str | None = None


class LoyalFanSignal(BaseModel):
    username: str
    loyalty_tier: str | None = None
    ambassador_score: float | None = None


class PlayerSignal(BaseModel):
    name: str
    handle: str | None = None


class BrandVoice(BaseModel):
    tone: list[str] = Field(default_factory=list)
    banned_words: list[str] = Field(default_factory=list)
    signature_phrases: list[str] = Field(default_factory=list)
    default_ctas: list[str] = Field(default_factory=list)
    forbidden_patterns: list[str] = Field(default_factory=list)


class SignalsPreview(BaseModel):
    seo_keywords: list[SeoSignal] = Field(default_factory=list)
    top_posts: list[TopPostSignal] = Field(default_factory=list)
    news: list[NewsSignal] = Field(default_factory=list)
    reddit: list[RedditSignal] = Field(default_factory=list)


# ─── Internal context bundle (assembled by signal_collectors) ─────────────────

class ContextBundle(BaseModel):
    """Internal bundle passed from signal collectors → prompt builder."""

    seo_keywords: list[SeoSignal] = Field(default_factory=list)
    top_posts: list[TopPostSignal] = Field(default_factory=list)
    news: list[NewsSignal] = Field(default_factory=list)
    reddit: list[RedditSignal] = Field(default_factory=list)
    loyal_fans: list[LoyalFanSignal] = Field(default_factory=list)
    players: list[PlayerSignal] = Field(default_factory=list)
    brand_voice: BrandVoice = Field(default_factory=BrandVoice)
    focus_news_article: NewsSignal | None = None


# ─── API request/response ─────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    content_type: ContentType
    template_id: UUID | None = None
    signals_config: SignalsConfig = Field(default_factory=SignalsConfig)
    source_article_id: UUID | None = None
    instructions: str | None = None
    tone: Tone = Tone.INFORMATIVE
    length: Length = Length.MEDIUM
    audience: Audience = Audience.GENERAL_FANS
    cta_goal: CtaGoal = CtaGoal.NONE
    created_by: str = "anon@joola.com"  # v1 — no auth

    @field_validator("instructions")
    @classmethod
    def _strip_instructions(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


class GenerateResponse(BaseModel):
    draft_id: UUID
    body: str
    title: str | None = None
    hashtags: list[str] = Field(default_factory=list)
    run_id: UUID
    cost_usd: float = 0.0


class Draft(BaseModel):
    id: UUID
    created_at: datetime
    updated_at: datetime
    created_by: str
    content_type: str
    status: str
    title: str | None = None
    body: str
    hashtags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_article_id: UUID | None = None
    source_signal_snapshot: dict[str, Any] = Field(default_factory=dict)
    generation_run_id: UUID | None = None
    parent_draft_id: UUID | None = None
    version: int = 1


class DraftUpdate(BaseModel):
    body: str | None = None
    title: str | None = None
    hashtags: list[str] | None = None
    status: DraftStatus | None = None


class DraftListResponse(BaseModel):
    drafts: list[Draft] = Field(default_factory=list)
    total: int = 0


class Template(BaseModel):
    id: UUID
    name: str
    content_type: str
    system_prompt: str
    user_prompt_template: str
    is_active: bool = True
    created_at: datetime


class CriticResult(BaseModel):
    passed: bool = True
    violations: list[str] = Field(default_factory=list)
    suggested_fix: str = ""


class RegenerateRequest(BaseModel):
    instructions: str | None = None


# ─── SSE event payloads ──────────────────────────────────────────────────────

class SseMeta(BaseModel):
    """Initial 'meta' event sent after the prompt is built."""

    run_id: UUID
    content_type: str
    model: str
    variant_count: int


class SseDone(BaseModel):
    """Final 'done' event with the saved draft id."""

    draft_id: UUID
    run_id: UUID
    body: str
    title: str | None = None
    hashtags: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    critic: CriticResult = Field(default_factory=CriticResult)
