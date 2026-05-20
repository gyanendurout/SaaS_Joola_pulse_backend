"""Signal collectors — one async fn per source.

Each collector queries Supabase per spec §5, caches the result in-process,
and returns its slice of the ContextBundle.

The Supabase client is sync; we wrap calls with `asyncio.to_thread` so
`asyncio.gather` can run collectors in parallel without blocking the loop.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from app.content.types import (
    BrandVoice,
    ContextBundle,
    LoyalFanSignal,
    NewsSignal,
    PlayerSignal,
    RedditSignal,
    SeoSignal,
    SignalsConfig,
    TopPostSignal,
)
from app.db import service_client

log = structlog.get_logger()

# ─── In-process TTL cache ─────────────────────────────────────────────────────

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = asyncio.Lock()


async def _cached(key: str, ttl_seconds: int, loader):
    async with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            ts, val = hit
            if time.time() - ts < ttl_seconds:
                return val
    val = await loader()
    async with _CACHE_LOCK:
        _CACHE[key] = (time.time(), val)
    return val


def clear_cache() -> None:
    """Test helper — wipe the in-process cache."""
    _CACHE.clear()


# ─── Per-source collectors ────────────────────────────────────────────────────

import re as _re

# JOOLA Pulse is pickleball-only. JOOLA the brand also sells table-tennis gear,
# so domain_ranked_keywords is full of TT terms that we must NOT surface here.
# Must mirror the frontend filter in app/content-generation/text/page.tsx.
_PICKLEBALL_KW = _re.compile(
    r"\b(pickle\s*ball|pickleball|ben\s*johns|anna\s*leigh|tyson\s*mcguffin|"
    r"hyperion|perseus|joola)\b",
    _re.IGNORECASE,
)
_TT_KW = _re.compile(
    r"\b(table[\s-]?tennis|tennis[\s-]?table|ping[\s-]?pong|pong[\s-]?ping|"
    r"\btt\b|tt[\s-]?(?:table|ball|rubber|blade)|"
    r"(?:foldable|outdoor|indoor)\s+tt|"
    r"tennis\s+(?:equipment|sport|racket|racquet|paddle|ball|rubber|blade)|"
    r"stiga|butterfly|killerspin)\b",
    _re.IGNORECASE,
)


def _q_seo_keywords_sync() -> list[SeoSignal]:
    db = service_client()
    try:
        res = (
            db.table("domain_ranked_keywords")
            .select("keyword,search_volume,position")
            .order("search_volume", desc=True)
            .limit(400)
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        log.warning("seo_keywords_query_failed", error=str(e))
        return []
    # Dedupe + apply pickleball-only filter so picked keywords like
    # "pickleball nets" are reachable instead of crowded out by TT terms.
    seen: dict[str, dict] = {}
    for r in rows:
        k = (r.get("keyword") or "").strip().lower()
        if not k:
            continue
        if _TT_KW.search(k):
            continue
        if not _PICKLEBALL_KW.search(k):
            continue
        prev = seen.get(k)
        if prev is None:
            seen[k] = r
            continue
        prev_vol = prev.get("search_volume") or 0
        cur_vol = r.get("search_volume") or 0
        if cur_vol > prev_vol:
            seen[k] = r
        elif cur_vol == prev_vol and (r.get("position") or 9999) < (prev.get("position") or 9999):
            seen[k] = r
    return [
        SeoSignal(
            keyword=r.get("keyword") or "",
            search_volume=r.get("search_volume"),
            position=r.get("position"),
            is_gap=r.get("position") is None,
            difficulty=None,
        )
        for r in sorted(seen.values(), key=lambda r: -(r.get("search_volume") or 0))[:60]
    ]


async def collect_seo_keywords() -> list[SeoSignal]:
    """SEO keywords vol≥500 ORDER BY vol DESC LIMIT 20. Cached 6h."""
    return await _cached(
        "seo_keywords",
        6 * 3600,
        lambda: asyncio.to_thread(_q_seo_keywords_sync),
    )


# JOOLA brand_id — same constant used by the frontend page.tsx. Must match.
# Filters out competitor brands (Selkirk, Paddletek, Engage, etc.) that share
# the cross-platform tables. JOOLA-only because Content Studio drafts from
# our OWN posts.
_JOOLA_BRAND_ID = "04db8591-37a3-4634-9d11-536975fa6935"


def _q_top_posts_sync() -> list[TopPostSignal]:
    """Top posts across IG, TikTok, X, YouTube — JOOLA-owned accounts only.

    The user-selected `post_id` flows through `assemble_bundle`'s filter
    (`selected_post_ids`); for that to work we must include the matching
    row regardless of which platform it came from.
    """
    db = service_client()
    out: list[TopPostSignal] = []

    # 1) Instagram — table is JOOLA-only by design
    try:
        res = (
            db.table("joola_ig_posts")
            .select(
                "post_id,engagement_rate,thumbnail_url,caption,posted_at,post_type,"
                "like_count,view_count,comment_count,post_url,"
                "joola_ig_post_analysis(content_theme)"
            )
            .order("engagement_rate", desc=True, nullsfirst=False)
            .limit(15)
            .execute()
        )
        for r in res.data or []:
            cap = r.get("caption") or ""
            first_line = cap.splitlines()[0][:200] if cap else None
            analysis = r.get("joola_ig_post_analysis") or []
            theme = None
            if isinstance(analysis, list) and analysis:
                theme = analysis[0].get("content_theme")
            elif isinstance(analysis, dict):
                theme = analysis.get("content_theme")
            out.append(TopPostSignal(
                post_id=str(r.get("post_id") or ""),
                platform="instagram",
                content_theme=theme,
                engagement_rate=r.get("engagement_rate"),
                likes=r.get("like_count"),
                views=r.get("view_count"),
                comments=r.get("comment_count"),
                caption_first_line=first_line,
                thumbnail_url=r.get("thumbnail_url"),
                post_type=r.get("post_type"),
                posted_at=str(r.get("posted_at")) if r.get("posted_at") else None,
                url=r.get("post_url"),
            ))
    except Exception as e:
        log.warning("top_posts_ig_query_failed", error=str(e))

    # 2) TikTok — filter to JOOLA brand_id
    try:
        res = (
            db.table("tiktok_videos")
            .select(
                "id,tiktok_video_id,text,thumbnail_url,video_url,"
                "view_count,like_count,comment_count,share_count,posted_at,topics"
            )
            .eq("brand_id", _JOOLA_BRAND_ID)
            .order("view_count", desc=True, nullsfirst=False)
            .limit(15)
            .execute()
        )
        for r in res.data or []:
            text = r.get("text") or ""
            first_line = text.splitlines()[0][:200] if text else None
            topics = r.get("topics") or []
            theme = topics[0] if isinstance(topics, list) and topics else None
            views = r.get("view_count")
            likes = r.get("like_count")
            er = (likes / views) if views and likes else None
            out.append(TopPostSignal(
                post_id=str(r.get("id") or ""),
                platform="tiktok",
                content_theme=theme,
                engagement_rate=er,
                likes=likes,
                views=views,
                comments=r.get("comment_count"),
                caption_first_line=first_line,
                thumbnail_url=r.get("thumbnail_url"),
                post_type="video",
                posted_at=str(r.get("posted_at")) if r.get("posted_at") else None,
                url=r.get("video_url"),
            ))
    except Exception as e:
        log.warning("top_posts_tiktok_query_failed", error=str(e))

    # 3) X / Twitter — filter to JOOLA brand_id
    try:
        res = (
            db.table("x_posts")
            .select("id,text,like_count,retweet_count,reply_count,view_count,posted_at")
            .eq("brand_id", _JOOLA_BRAND_ID)
            .order("like_count", desc=True, nullsfirst=False)
            .limit(15)
            .execute()
        )
        for r in res.data or []:
            text = r.get("text") or ""
            first_line = text.splitlines()[0][:200] if text else None
            views = r.get("view_count")
            likes = r.get("like_count")
            er = (likes / views) if views and likes else None
            out.append(TopPostSignal(
                post_id=str(r.get("id") or ""),
                platform="twitter",
                content_theme=None,
                engagement_rate=er,
                likes=likes,
                views=views,
                comments=r.get("reply_count"),
                caption_first_line=first_line,
                thumbnail_url=None,
                post_type="tweet",
                posted_at=str(r.get("posted_at")) if r.get("posted_at") else None,
                url=None,
            ))
    except Exception as e:
        log.warning("top_posts_x_query_failed", error=str(e))

    # 4) YouTube — filter to JOOLA brand_id
    try:
        res = (
            db.table("yt_videos")
            .select(
                "id,youtube_video_id,title,thumbnail_url,"
                "view_count,like_count,comment_count,published_at"
            )
            .eq("brand_id", _JOOLA_BRAND_ID)
            .order("view_count", desc=True, nullsfirst=False)
            .limit(15)
            .execute()
        )
        for r in res.data or []:
            title = r.get("title") or ""
            views = r.get("view_count")
            likes = r.get("like_count")
            er = (likes / views) if views and likes else None
            yt_id = r.get("youtube_video_id")
            out.append(TopPostSignal(
                post_id=str(r.get("id") or ""),
                platform="youtube",
                content_theme=None,
                engagement_rate=er,
                likes=likes,
                views=views,
                comments=r.get("comment_count"),
                caption_first_line=title,
                thumbnail_url=r.get("thumbnail_url"),
                post_type="video",
                posted_at=str(r.get("published_at")) if r.get("published_at") else None,
                url=f"https://www.youtube.com/watch?v={yt_id}" if yt_id else None,
            ))
    except Exception as e:
        log.warning("top_posts_yt_query_failed", error=str(e))

    return out


async def collect_top_posts() -> list[TopPostSignal]:
    """Top JOOLA posts across IG, TikTok, X, YouTube (15 each). Cached 1h."""
    return await _cached(
        "top_posts",
        3600,
        lambda: asyncio.to_thread(_q_top_posts_sync),
    )


def _q_news_sync(source_article_id: str | None) -> list[NewsSignal]:
    db = service_client()
    try:
        q = (
            db.table("news_articles")
            .select(
                "id,title,ai_summary,why_it_matters,players_mentioned,"
                "suggested_action,sentiment,is_joola_mention,importance_score,published_at"
            )
            .order("importance_score", desc=True)
            .limit(20)
        )
        res = q.execute()
        rows = res.data or []
        # If focus article requested, ensure it's first
        if source_article_id:
            try:
                focus = (
                    db.table("news_articles")
                    .select(
                        "id,title,ai_summary,why_it_matters,players_mentioned,"
                        "suggested_action,sentiment,is_joola_mention,importance_score,published_at"
                    )
                    .eq("id", source_article_id)
                    .single()
                    .execute()
                )
                if focus.data:
                    rows = [focus.data] + [r for r in rows if r.get("id") != source_article_id]
            except Exception:
                pass
    except Exception as e:
        log.warning("news_query_failed", error=str(e))
        return []

    return [
        NewsSignal(
            id=str(r.get("id") or ""),
            title=r.get("title") or "",
            ai_summary=r.get("ai_summary"),
            why_it_matters=r.get("why_it_matters"),
            players_mentioned=list(r.get("players_mentioned") or []),
            suggested_action=r.get("suggested_action"),
            sentiment=r.get("sentiment"),
            is_joola_mention=bool(r.get("is_joola_mention")),
        )
        for r in rows if r.get("id")
    ]


async def collect_news(source_article_id: str | None = None) -> list[NewsSignal]:
    """News articles ORDER BY importance_score. Not cached (live)."""
    return await asyncio.to_thread(_q_news_sync, source_article_id)


def _q_reddit_sync() -> list[RedditSignal]:
    db = service_client()
    try:
        # 14d lookback
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        res = (
            db.table("reddit_mentions")
            .select("id,post_title,subreddit,topics,sentiment,is_crisis,is_opportunity,content_text")
            .or_("is_opportunity.eq.true,is_crisis.eq.true")
            .gte("posted_at", cutoff)
            .limit(10)
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        log.warning("reddit_query_failed", error=str(e))
        return []

    out: list[RedditSignal] = []
    for r in rows:
        content = r.get("content_text") or ""
        excerpt = content[:160] + "…" if len(content) > 160 else content or None
        # Comments inherit no post_title; fall back to first line of content so UI rows aren't blank.
        title = r.get("post_title") or (content.splitlines()[0][:80] if content else "(Reddit comment)")
        out.append(RedditSignal(
            id=str(r.get("id") or ""),
            title=title,
            subreddit=r.get("subreddit"),
            topics=list(r.get("topics") or []),
            sentiment=r.get("sentiment"),
            is_crisis=bool(r.get("is_crisis")),
            excerpt=excerpt,
        ))
    return out


async def collect_reddit() -> list[RedditSignal]:
    """Reddit mentions is_opportunity OR is_crisis last 14d LIMIT 10. Cached 6h."""
    return await _cached(
        "reddit",
        6 * 3600,
        lambda: asyncio.to_thread(_q_reddit_sync),
    )


def _q_loyal_fans_sync() -> list[LoyalFanSignal]:
    db = service_client()
    try:
        res = (
            db.table("joola_ig_loyal_users")
            .select("username,loyalty_tier,ambassador_score")
            .order("ambassador_score", desc=True)
            .limit(5)
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        log.warning("loyal_fans_query_failed", error=str(e))
        return []
    return [
        LoyalFanSignal(
            username=r.get("username") or "",
            loyalty_tier=r.get("loyalty_tier"),
            ambassador_score=r.get("ambassador_score"),
        )
        for r in rows if r.get("username")
    ]


async def collect_loyal_fans() -> list[LoyalFanSignal]:
    """Top 5 loyal users by ambassador_score. Cached daily."""
    return await _cached(
        "loyal_fans",
        24 * 3600,
        lambda: asyncio.to_thread(_q_loyal_fans_sync),
    )


def _q_player_roster_sync() -> list[PlayerSignal]:
    db = service_client()
    try:
        res = (
            db.table("influencers")
            .select("name,instagram_handle,is_active")
            .eq("is_active", True)
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        log.warning("player_roster_query_failed", error=str(e))
        return []
    out: list[PlayerSignal] = []
    for r in rows:
        name = r.get("name") or ""
        if not name:
            continue
        out.append(PlayerSignal(name=name, handle=r.get("instagram_handle")))
    return out


async def collect_player_roster() -> list[PlayerSignal]:
    """JOOLA roster influencers. Cached daily."""
    return await _cached(
        "player_roster",
        24 * 3600,
        lambda: asyncio.to_thread(_q_player_roster_sync),
    )


def _q_brand_voice_sync() -> BrandVoice:
    db = service_client()
    try:
        res = db.table("content_brand_voice").select("*").limit(1).execute()
        rows = res.data or []
        if not rows:
            return BrandVoice()
        r = rows[0]
        return BrandVoice(
            tone=list(r.get("tone") or []),
            banned_words=list(r.get("banned_words") or []),
            signature_phrases=list(r.get("signature_phrases") or []),
            default_ctas=list(r.get("default_ctas") or []),
            forbidden_patterns=list(r.get("forbidden_patterns") or []),
        )
    except Exception as e:
        log.warning("brand_voice_query_failed", error=str(e))
        return BrandVoice()


async def collect_brand_voice() -> BrandVoice:
    """Single-row brand voice config. No TTL; manually invalidated."""
    return await asyncio.to_thread(_q_brand_voice_sync)


# ─── Orchestrator ─────────────────────────────────────────────────────────────

async def assemble_bundle(
    config: SignalsConfig,
    source_article_id: str | None = None,
) -> ContextBundle:
    """Run enabled collectors in parallel via asyncio.gather and assemble bundle."""

    async def _seo():
        return await collect_seo_keywords() if config.seo_keywords else []

    async def _posts():
        return await collect_top_posts() if config.top_posts else []

    async def _news():
        return await collect_news(source_article_id) if (config.news or source_article_id) else []

    async def _reddit():
        return await collect_reddit() if config.reddit else []

    async def _fans():
        return await collect_loyal_fans() if config.loyal_fans else []

    async def _players():
        return await collect_player_roster() if config.player_roster else []

    seo, posts, news, reddit, fans, players, voice = await asyncio.gather(
        _seo(), _posts(), _news(), _reddit(), _fans(), _players(),
        collect_brand_voice(),
    )

    # Filter by selected IDs if provided
    if config.selected_keyword_ids:
        wanted = set(config.selected_keyword_ids)
        seo = [s for s in seo if s.keyword in wanted]
    if config.selected_post_ids:
        wanted = set(config.selected_post_ids)
        posts = [p for p in posts if p.post_id in wanted]
    if config.selected_news_ids:
        wanted = set(config.selected_news_ids)
        news = [n for n in news if n.id in wanted]
    if config.selected_reddit_ids:
        wanted = set(config.selected_reddit_ids)
        reddit = [r for r in reddit if r.id in wanted]

    focus = None
    if source_article_id:
        focus = next((n for n in news if n.id == source_article_id), None)

    return ContextBundle(
        seo_keywords=seo,
        top_posts=posts,
        news=news,
        reddit=reddit,
        loyal_fans=fans,
        players=players,
        brand_voice=voice,
        focus_news_article=focus,
    )


async def preview_signals(source_article_id: str | None = None):
    """Quick read of all sources for the SignalsPreview API."""
    from app.content.types import SignalsPreview

    seo, posts, news, reddit = await asyncio.gather(
        collect_seo_keywords(),
        collect_top_posts(),
        collect_news(source_article_id),
        collect_reddit(),
    )
    return SignalsPreview(
        seo_keywords=seo,
        top_posts=posts,
        news=news,
        reddit=reddit,
    )
