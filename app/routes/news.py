"""News API routes — articles, analytics, scrape trigger, SSE progress."""
from __future__ import annotations

import json
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from io import StringIO
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.responses import StreamingResponse

from app.agents.news_scraper import scrape_all_sites, JOOLA_PLAYERS, COMPETITOR_BRANDS
from app.db import service_client
from app.services import event_bus

router = APIRouter(prefix="/api/news", tags=["news"])


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cutoff_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _prev_cutoff_iso(days: int) -> str:
    """Start of previous period (2× days ago)."""
    return (datetime.now(timezone.utc) - timedelta(days=days * 2)).isoformat()

# ============================================================================ #
# Scrape endpoints                                                               #
# ============================================================================ #

@router.post("/scrape")
async def trigger_scrape(background_tasks: BackgroundTasks) -> dict[str, str]:
    run_id = str(uuid.uuid4())
    db = service_client()
    db.table("news_scrape_runs").insert({
        "id": run_id,
        "status": "pending",
        "run_type": "manual",
        "lookback_days": 180,
        "sites_total": 20,
        "sites_scraped": 0,
        "articles_found": 0,
        "articles_new": 0,
        "articles_with_mentions": 0,
        "created_at": _utcnow(),
    }).execute()
    background_tasks.add_task(scrape_all_sites, run_id)
    return {"run_id": run_id}


@router.get("/scrape/latest")
async def latest_scrape() -> dict[str, Any]:
    db = service_client()
    res = (
        db.table("news_scrape_runs")
        .select("*")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else {}


@router.get("/scrape/runs")
async def list_scrape_runs(limit: int = Query(20, ge=1, le=100)) -> list[dict[str, Any]]:
    db = service_client()
    res = (
        db.table("news_scrape_runs")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


@router.get("/scrape/{run_id}/events")
async def scrape_events(run_id: str) -> StreamingResponse:
    async def _gen():
        async for evt in event_bus.subscribe(run_id):
            yield f"data: {json.dumps(evt)}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ============================================================================ #
# Article list endpoint                                                          #
# ============================================================================ #

@router.get("/articles")
async def list_articles(
    q: str | None = Query(None),
    sentiment: str | None = Query(None),
    mention: str | None = Query(None),
    player: str | None = Query(None),
    source: str | None = Query(None),
    article_type: str | None = Query(None),
    relevance_type: str | None = Query(None),
    suggested_action: str | None = Query(None),
    competitor: str | None = Query(None),
    importance_min: float | None = Query(None, ge=0, le=100),
    days: int = Query(180, ge=1, le=730),
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = service_client()
    cutoff = _cutoff_iso(days)

    query = (
        db.table("news_articles")
        .select("*")
        .eq("is_active", True)
        .gte("published_at", cutoff)
        .order("published_at", desc=True)
    )

    if sentiment:
        query = query.eq("sentiment", sentiment)
    if mention == "joola":
        query = query.eq("is_joola_mention", True)
    if mention == "competitor":
        query = query.eq("has_competitor_mention", True)
    if source:
        query = query.eq("source_site", source)
    if article_type:
        query = query.eq("article_type", article_type)
    if relevance_type:
        query = query.eq("relevance_type", relevance_type)
    if suggested_action:
        query = query.eq("suggested_action", suggested_action)
    if importance_min is not None:
        query = query.gte("importance_score", importance_min)

    query = query.range(offset, offset + limit - 1)
    res = query.execute()
    articles: list[dict[str, Any]] = res.data or []

    # Python-side filters for array columns
    if player:
        articles = [a for a in articles if player in (a.get("players_mentioned") or [])]
    if competitor:
        articles = [a for a in articles if competitor in (a.get("competitors_mentioned") or [])]
    if mention == "player":
        articles = [a for a in articles if a.get("players_mentioned")]
    if mention == "any":
        articles = [a for a in articles if a.get("is_joola_mention") or a.get("players_mentioned")]
    if mention == "both":
        articles = [a for a in articles if a.get("is_joola_mention") and a.get("players_mentioned")]
    if q:
        q_l = q.lower()
        articles = [
            a for a in articles
            if q_l in (a.get("title") or "").lower()
            or q_l in (a.get("excerpt") or "").lower()
            or q_l in (a.get("ai_summary") or "").lower()
        ]

    # Stats for display
    all_res = (
        db.table("news_articles")
        .select("sentiment, is_joola_mention, players_mentioned, article_type, published_at, scraped_at")
        .eq("is_active", True)
        .gte("published_at", cutoff)
        .execute()
    )
    all_rows = all_res.data or []
    total_all = len(all_rows)
    joola_count = sum(1 for r in all_rows if r.get("is_joola_mention"))
    player_count = sum(1 for r in all_rows if r.get("players_mentioned"))
    week_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    new_this_week = sum(1 for r in all_rows if r.get("scraped_at") and r["scraped_at"] >= week_cutoff)
    sentiment_counts = Counter(r.get("sentiment") for r in all_rows if r.get("sentiment"))

    return {
        "articles": articles,
        "total": len(articles),
        "stats": {
            "total": total_all,
            "joola_mentions": joola_count,
            "player_mentions": player_count,
            "new_this_week": new_this_week,
            "sentiment": dict(sentiment_counts),
        },
    }


@router.get("/articles/{article_id}")
async def get_article(article_id: str) -> dict[str, Any]:
    db = service_client()
    res = db.table("news_articles").select("*").eq("id", article_id).execute()
    if not res.data:
        return {}
    article = res.data[0]

    # Fetch related articles (same source or same players)
    related: list[dict[str, Any]] = []
    try:
        source = article.get("source_site")
        rel_res = (
            db.table("news_articles")
            .select("id, title, url, published_at, sentiment, source_site, importance_score")
            .eq("source_site", source)
            .eq("is_active", True)
            .neq("id", article_id)
            .order("published_at", desc=True)
            .limit(5)
            .execute()
        )
        related = rel_res.data or []
    except Exception:
        pass

    return {"article": article, "related": related}

# ============================================================================ #
# Analytics endpoints                                                            #
# ============================================================================ #

@router.get("/analytics/summary")
async def analytics_summary(days: int = Query(180, ge=1, le=730)) -> dict[str, Any]:
    """KPI summary for the dashboard header."""
    db = service_client()
    cutoff = _cutoff_iso(days)
    prev_cutoff = _prev_cutoff_iso(days)

    # Current period
    curr_res = (
        db.table("news_articles")
        .select(
            "sentiment, is_joola_mention, players_mentioned, "
            "has_competitor_mention, importance_score, source_site, "
            "relevance_type, suggested_action, published_at"
        )
        .eq("is_active", True)
        .gte("published_at", cutoff)
        .execute()
    )
    curr = curr_res.data or []

    # Previous period (for trend calculation)
    prev_res = (
        db.table("news_articles")
        .select("sentiment, is_joola_mention, players_mentioned, published_at")
        .eq("is_active", True)
        .gte("published_at", prev_cutoff)
        .lt("published_at", cutoff)
        .execute()
    )
    prev = prev_res.data or []

    def _pct_change(curr_val: int, prev_val: int) -> float | None:
        if prev_val == 0:
            return None
        return round((curr_val - prev_val) / prev_val * 100, 1)

    total = len(curr)
    joola_count = sum(1 for r in curr if r.get("is_joola_mention"))
    player_count = sum(1 for r in curr if r.get("players_mentioned"))
    positive_count = sum(1 for r in curr if r.get("sentiment") == "positive")
    negative_count = sum(1 for r in curr if r.get("sentiment") in ("negative", "risk"))
    risk_count = sum(1 for r in curr if r.get("sentiment") == "risk")
    informative_count = sum(1 for r in curr if r.get("sentiment") == "informative")
    competitor_count = sum(1 for r in curr if r.get("has_competitor_mention"))
    avg_importance = (
        round(sum(r.get("importance_score") or 0 for r in curr) / total, 1)
        if total else 0.0
    )

    # Previous period counts for trends
    prev_joola = sum(1 for r in prev if r.get("is_joola_mention"))
    prev_player = sum(1 for r in prev if r.get("players_mentioned"))
    prev_total = len(prev)

    # Top source
    source_counts = Counter(r.get("source_site") for r in curr if r.get("source_site"))
    top_source = source_counts.most_common(1)[0][0] if source_counts else None

    # Top player
    player_counts: dict[str, int] = defaultdict(int)
    for r in curr:
        for p in (r.get("players_mentioned") or []):
            player_counts[p] += 1
    top_player = max(player_counts, key=lambda p: player_counts[p]) if player_counts else None

    # Action breakdown
    action_counts = Counter(r.get("suggested_action") for r in curr if r.get("suggested_action"))
    risk_review_count = action_counts.get("Risk review", 0)
    share_marketing_count = action_counts.get("Share with marketing", 0)

    return {
        "period_days": days,
        "total_articles": total,
        "joola_related": joola_count,
        "player_mentions": player_count,
        "positive_count": positive_count,
        "negative_count": negative_count,
        "risk_count": risk_count,
        "informative_count": informative_count,
        "competitor_count": competitor_count,
        "avg_importance": avg_importance,
        "top_source": top_source,
        "top_player": top_player,
        "risk_review_count": risk_review_count,
        "share_marketing_count": share_marketing_count,
        "trends": {
            "joola_pct_change": _pct_change(joola_count, prev_joola),
            "player_pct_change": _pct_change(player_count, prev_player),
            "total_pct_change": _pct_change(total, prev_total),
        },
        "sentiment_breakdown": {
            "positive": positive_count,
            "negative": negative_count,
            "risk": risk_count,
            "informative": informative_count,
            "neutral": sum(1 for r in curr if r.get("sentiment") == "neutral"),
            "mixed": sum(1 for r in curr if r.get("sentiment") == "mixed"),
        },
        "relevance_breakdown": dict(Counter(
            r.get("relevance_type") for r in curr if r.get("relevance_type")
        )),
        "action_breakdown": dict(action_counts),
    }


@router.get("/analytics/players")
async def analytics_players(days: int = Query(180, ge=1, le=730)) -> list[dict[str, Any]]:
    """Player mention leaderboard."""
    db = service_client()
    cutoff = _cutoff_iso(days)

    res = (
        db.table("news_articles")
        .select("players_mentioned, sentiment, sentiment_score, importance_score, "
                "source_site, published_at, title, url, id")
        .eq("is_active", True)
        .gte("published_at", cutoff)
        .execute()
    )
    rows = res.data or []

    # Build per-player stats
    player_data: dict[str, dict[str, Any]] = {}
    for r in rows:
        for player in (r.get("players_mentioned") or []):
            if player not in player_data:
                player_data[player] = {
                    "player": player,
                    "total_mentions": 0,
                    "positive": 0,
                    "negative": 0,
                    "risk": 0,
                    "informative": 0,
                    "sentiment_scores": [],
                    "importance_scores": [],
                    "latest_article": None,
                    "latest_date": None,
                    "top_source": None,
                    "_sources": [],
                }
            pd = player_data[player]
            pd["total_mentions"] += 1
            sent = r.get("sentiment") or "informative"
            if sent == "positive":
                pd["positive"] += 1
            elif sent in ("negative", "risk"):
                pd["negative"] += 1
            elif sent == "risk":
                pd["risk"] += 1
            else:
                pd["informative"] += 1
            if r.get("sentiment_score") is not None:
                pd["sentiment_scores"].append(r["sentiment_score"])
            if r.get("importance_score") is not None:
                pd["importance_scores"].append(r["importance_score"])
            if r.get("source_site"):
                pd["_sources"].append(r["source_site"])
            pub = r.get("published_at")
            if pub and (pd["latest_date"] is None or pub > pd["latest_date"]):
                pd["latest_date"] = pub
                pd["latest_article"] = {"title": r.get("title"), "url": r.get("url"), "id": r.get("id")}

    results = []
    for pd in player_data.values():
        scores = pd.pop("sentiment_scores")
        imp_scores = pd.pop("importance_scores")
        sources = pd.pop("_sources")
        pd["avg_sentiment"] = round(sum(scores) / len(scores), 2) if scores else 0.0
        pd["avg_importance"] = round(sum(imp_scores) / len(imp_scores), 1) if imp_scores else 0.0
        if sources:
            src_counter = Counter(sources)
            pd["top_source"] = src_counter.most_common(1)[0][0]
        results.append(pd)

    results.sort(key=lambda x: -x["total_mentions"])
    return results


@router.get("/analytics/sources")
async def analytics_sources(days: int = Query(180, ge=1, le=730)) -> list[dict[str, Any]]:
    """Source coverage stats."""
    db = service_client()
    cutoff = _cutoff_iso(days)

    articles_res = (
        db.table("news_articles")
        .select("source_site, sentiment, is_joola_mention, has_competitor_mention, "
                "importance_score, players_mentioned, scraped_at")
        .eq("is_active", True)
        .gte("published_at", cutoff)
        .execute()
    )
    rows = articles_res.data or []

    # Source stats
    source_data: dict[str, dict[str, Any]] = {}
    for r in rows:
        src = r.get("source_site") or "unknown"
        if src not in source_data:
            source_data[src] = {
                "source": src,
                "total_articles": 0,
                "joola_mentions": 0,
                "player_mentions": 0,
                "competitor_mentions": 0,
                "positive": 0,
                "negative": 0,
                "informative": 0,
                "importance_scores": [],
            }
        sd = source_data[src]
        sd["total_articles"] += 1
        if r.get("is_joola_mention"):
            sd["joola_mentions"] += 1
        if r.get("players_mentioned"):
            sd["player_mentions"] += 1
        if r.get("has_competitor_mention"):
            sd["competitor_mentions"] += 1
        sent = r.get("sentiment") or "informative"
        if sent == "positive":
            sd["positive"] += 1
        elif sent in ("negative", "risk"):
            sd["negative"] += 1
        else:
            sd["informative"] += 1
        if r.get("importance_score") is not None:
            sd["importance_scores"].append(r["importance_score"])

    # Fetch health from news_sources
    try:
        health_res = db.table("news_sources").select("name, last_success_at, last_failed_at, last_error, is_active").execute()
        health_map = {h["name"]: h for h in (health_res.data or [])}
    except Exception:
        health_map = {}

    results = []
    for sd in source_data.values():
        imp_scores = sd.pop("importance_scores")
        sd["avg_importance"] = round(sum(imp_scores) / len(imp_scores), 1) if imp_scores else 0.0
        health = health_map.get(sd["source"], {})
        sd["last_success_at"] = health.get("last_success_at")
        sd["last_failed_at"] = health.get("last_failed_at")
        sd["last_error"] = health.get("last_error")
        sd["is_active"] = health.get("is_active", True)
        results.append(sd)

    results.sort(key=lambda x: -x["joola_mentions"])
    return results


@router.get("/analytics/trends")
async def analytics_trends(days: int = Query(90, ge=7, le=365)) -> dict[str, Any]:
    """Weekly article volume, JOOLA mentions, and sentiment trend."""
    db = service_client()
    cutoff = _cutoff_iso(days)

    res = (
        db.table("news_articles")
        .select("published_at, sentiment, is_joola_mention, players_mentioned, importance_score")
        .eq("is_active", True)
        .gte("published_at", cutoff)
        .order("published_at")
        .execute()
    )
    rows = res.data or []

    # Group by ISO week
    weekly: dict[str, dict[str, Any]] = {}
    for r in rows:
        pub = r.get("published_at")
        if not pub:
            continue
        try:
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            # Week starting Monday
            week_start = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
        except Exception:
            continue

        if week_start not in weekly:
            weekly[week_start] = {
                "week": week_start,
                "total": 0,
                "joola": 0,
                "player": 0,
                "positive": 0,
                "negative": 0,
                "informative": 0,
            }
        w = weekly[week_start]
        w["total"] += 1
        if r.get("is_joola_mention"):
            w["joola"] += 1
        if r.get("players_mentioned"):
            w["player"] += 1
        sent = r.get("sentiment") or "informative"
        if sent == "positive":
            w["positive"] += 1
        elif sent in ("negative", "risk"):
            w["negative"] += 1
        else:
            w["informative"] += 1

    weeks = sorted(weekly.values(), key=lambda x: x["week"])
    return {
        "period_days": days,
        "weeks": weeks,
        "totals": {
            "articles": sum(w["total"] for w in weeks),
            "joola": sum(w["joola"] for w in weeks),
            "player": sum(w["player"] for w in weeks),
        },
    }

# ============================================================================ #
# Filters helper                                                                 #
# ============================================================================ #

@router.get("/filters")
async def get_filters(days: int = Query(180)) -> dict[str, Any]:
    db = service_client()
    cutoff = _cutoff_iso(days)
    res = (
        db.table("news_articles")
        .select("source_site, sentiment, article_type, relevance_type, suggested_action, players_mentioned")
        .eq("is_active", True)
        .gte("published_at", cutoff)
        .execute()
    )
    rows = res.data or []

    sources = sorted({r["source_site"] for r in rows if r.get("source_site")})
    sentiments = sorted({r["sentiment"] for r in rows if r.get("sentiment")})
    types = sorted({r["article_type"] for r in rows if r.get("article_type")})
    relevance_types = sorted({r["relevance_type"] for r in rows if r.get("relevance_type")})
    actions = sorted({r["suggested_action"] for r in rows if r.get("suggested_action")})

    players_in_db: set[str] = set()
    for r in rows:
        for p in (r.get("players_mentioned") or []):
            players_in_db.add(p)

    return {
        "sources": sources,
        "sentiments": sentiments,
        "article_types": types,
        "relevance_types": relevance_types,
        "suggested_actions": actions,
        "players_with_mentions": sorted(players_in_db),
        "all_players": JOOLA_PLAYERS,
        "competitors": COMPETITOR_BRANDS,
    }

# ============================================================================ #
# Sources list                                                                   #
# ============================================================================ #

@router.get("/sources")
async def list_sources() -> list[str]:
    db = service_client()
    res = (
        db.table("news_articles")
        .select("source_site")
        .eq("is_active", True)
        .execute()
    )
    return sorted({r["source_site"] for r in (res.data or []) if r.get("source_site")})

# ============================================================================ #
# Stats (legacy — kept for backward compat)                                     #
# ============================================================================ #

@router.get("/stats")
async def get_stats(days: int = Query(180)) -> dict[str, Any]:
    db = service_client()
    cutoff = _cutoff_iso(days)
    res = (
        db.table("news_articles")
        .select("sentiment, is_joola_mention, players_mentioned, article_type, source_site, published_at")
        .eq("is_active", True)
        .gte("published_at", cutoff)
        .execute()
    )
    rows = res.data or []

    total = len(rows)
    joola = sum(1 for r in rows if r.get("is_joola_mention"))
    player = sum(1 for r in rows if r.get("players_mentioned"))
    sentiment_counts = Counter(r.get("sentiment") for r in rows if r.get("sentiment"))
    type_counts = Counter(r.get("article_type") for r in rows if r.get("article_type"))
    source_counts = Counter(r.get("source_site") for r in rows if r.get("source_site"))

    player_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        for p in (r.get("players_mentioned") or []):
            player_counts[p] += 1

    week_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    week_res = (
        db.table("news_articles")
        .select("id", count="exact")
        .eq("is_active", True)
        .gte("scraped_at", week_cutoff)
        .execute()
    )

    return {
        "total": total,
        "joola_mentions": joola,
        "player_mentions": player,
        "new_this_week": week_res.count or 0,
        "sentiment": dict(sentiment_counts),
        "types": dict(type_counts),
        "sources": dict(source_counts.most_common(20)),
        "top_players": dict(sorted(player_counts.items(), key=lambda x: -x[1])[:10]),
    }

# ============================================================================ #
# Export                                                                         #
# ============================================================================ #

@router.get("/export")
async def export_articles(
    sentiment: str | None = Query(None),
    mention: str | None = Query(None),
    source: str | None = Query(None),
    days: int = Query(180, ge=1, le=730),
) -> StreamingResponse:
    """Download filtered articles as CSV."""
    db = service_client()
    cutoff = _cutoff_iso(days)

    query = (
        db.table("news_articles")
        .select(
            "published_at, source_site, title, url, relevance_type, sentiment, "
            "sentiment_score, importance_score, is_joola_mention, players_mentioned, "
            "competitors_mentioned, ai_summary, why_it_matters, suggested_action, author"
        )
        .eq("is_active", True)
        .gte("published_at", cutoff)
        .order("published_at", desc=True)
        .limit(500)
    )
    if sentiment:
        query = query.eq("sentiment", sentiment)
    if source:
        query = query.eq("source_site", source)
    if mention == "joola":
        query = query.eq("is_joola_mention", True)

    rows = query.execute().data or []

    if mention == "player":
        rows = [r for r in rows if r.get("players_mentioned")]
    if mention == "any":
        rows = [r for r in rows if r.get("is_joola_mention") or r.get("players_mentioned")]

    output = StringIO()
    headers = [
        "Published Date", "Source", "Title", "URL", "Relevance Type",
        "Sentiment", "Sentiment Score", "Importance Score",
        "JOOLA Mentioned", "Players Mentioned", "Competitors Mentioned",
        "AI Summary", "Why It Matters", "Suggested Action", "Author",
    ]
    output.write(",".join(f'"{h}"' for h in headers) + "\n")

    for r in rows:
        def _cell(val: Any) -> str:
            if val is None:
                return '""'
            if isinstance(val, list):
                return '"' + "; ".join(str(v) for v in val).replace('"', '""') + '"'
            return '"' + str(val).replace('"', '""') + '"'

        output.write(",".join([
            _cell(r.get("published_at", "")[:10] if r.get("published_at") else ""),
            _cell(r.get("source_site")),
            _cell(r.get("title")),
            _cell(r.get("url")),
            _cell(r.get("relevance_type")),
            _cell(r.get("sentiment")),
            _cell(r.get("sentiment_score")),
            _cell(r.get("importance_score")),
            _cell("Yes" if r.get("is_joola_mention") else "No"),
            _cell(r.get("players_mentioned")),
            _cell(r.get("competitors_mentioned")),
            _cell(r.get("ai_summary")),
            _cell(r.get("why_it_matters")),
            _cell(r.get("suggested_action")),
            _cell(r.get("author")),
        ]) + "\n")

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=joola-news-{days}d.csv"},
    )
