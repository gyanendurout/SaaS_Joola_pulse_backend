"""
News scraper agent — crawls 20 pickleball media sites.

Stage 1: Fetch articles via RSS → HTML fallback
Stage 2: Entity detection (JOOLA, players, competitors)
Stage 3: Relevance classification + importance scoring + suggested action
Stage 4: AI enrichment for JOOLA-relevant articles (summary, why_it_matters)
Stage 5: Upsert to Supabase (idempotent via content_hash)
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import structlog
from bs4 import BeautifulSoup

from app.db import service_client
from app.services import event_bus

log = structlog.get_logger()

USER_AGENT = "JoolaNewsBot/1.0 (+internal; respects robots.txt)"
CUTOFF_DAYS = 180

# ============================================================================ #
# Entity constants                                                               #
# ============================================================================ #

JOOLA_PLAYERS: list[str] = [
    "Ben Johns", "Collin Johns", "Anna Bright", "Tyson McGuffin",
    "Federico Staksrud", "Simone Jardim", "Lea Jansen", "Lacy Schneemann",
    "Brooke Buckner", "Kate Fahey", "Milan Rane", "John Lucian Goins",
    "Patrick Smith", "Noe Khlif", "Alec LaMacchio", "Aanik Lohani",
    "Alka Strippoli", "Bobbi Oshiro", "Boone Casady", "Chuck Taylor",
    "Dayne Gingrich", "Jake Kusmider", "Johnny Goldberg", "Jonathan Truong",
    "Len Yang", "Luke Geiser", "Mota Alhouni", "Rachel Rettger",
    "Regina Franco Goldberg", "Ryder Brown", "Sammy Lee", "Scott Crandall",
    "Tam Trinh", "Wil Shaffer", "Zack Taylor",
]

COMPETITOR_BRANDS: list[str] = [
    "Selkirk", "Paddletek", "Franklin", "CRBN", "Engage",
    "Onix", "Six Zero", "Proton", "Head", "Wilson",
]

JOOLA_PRODUCT_KEYWORDS = {
    "perseus", "scorpeus", "hyperion", "agassi", "magnus",
    "joola paddle", "joola ball", "joola shoe", "joola bag",
    "joola apparel", "joola grip",
}

TOURNAMENT_KEYWORDS = {
    "ppa", "major league pickleball", "mlp", "app tour", "usa pickleball",
    "us open pickleball", "nationals", "dupr", "grand slam",
    "tournament", "championship", "open", "bracket", "finals",
    "gold medal", "silver medal", "bronze medal",
}

# Pre-compiled regex patterns
JOOLA_RE = re.compile(r'\bjoola\b', re.IGNORECASE)
PLAYER_RES: dict[str, re.Pattern[str]] = {
    p: re.compile(r'\b' + re.escape(p) + r'\b', re.IGNORECASE)
    for p in JOOLA_PLAYERS
}
COMPETITOR_RES: dict[str, re.Pattern[str]] = {
    b: re.compile(r'\b' + re.escape(b) + r'\b', re.IGNORECASE)
    for b in COMPETITOR_BRANDS
}

POSITIVE_WORDS = {
    "win", "wins", "won", "champion", "championship", "title",
    "victory", "victories", "triumph", "gold", "dominate", "dominates",
    "dominated", "amazing", "great", "best", "top", "impressive",
    "outstanding", "incredible", "excellent", "celebrated", "success",
    "breakthrough", "thrilled", "excited", "proud", "milestone", "record",
    "achievement", "honor", "award", "ranked", "historic", "crowned",
    "clinch", "clinches", "clinched", "advance", "advances", "advanced",
    "medal", "undefeated", "dominant", "praised", "endorsement",
}

NEGATIVE_WORDS = {
    "lose", "loss", "lost", "injury", "injured", "suspend", "suspended",
    "suspension", "banned", "ban", "controversy", "controversial", "scandal",
    "poor", "worst", "terrible", "disappointing", "upset", "crash",
    "eliminated", "disqualified", "withdraw", "withdrawn", "investigation",
    "allegation", "violation", "fine", "penalty", "decline", "concern",
    "problem", "failed", "failure", "crisis", "defeat", "defeated",
    "retires", "retiring", "criticism", "criticized",
}

RISK_WORDS = {
    "lawsuit", "legal", "controversy", "banned", "violation", "allegation",
    "investigation", "scandal", "backlash", "danger", "risk", "threat",
    "warning", "recall", "defect", "complaint", "harassment", "accusation",
    "negligence", "fraud",
}

# ============================================================================ #
# Site registry                                                                  #
# ============================================================================ #

@dataclass
class SiteConfig:
    name: str
    base_url: str
    rss_paths: list[str] = field(default_factory=lambda: [
        "/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml",
        "/feed/", "/rss/", "/news/feed", "/news/feed/", "/blog/feed",
    ])
    html_path: str = "/"
    authority_score: int = 50


SITES: list[SiteConfig] = [
    SiteConfig("pickleball.com",              "https://pickleball.com",               authority_score=80),
    SiteConfig("thedinkpickleball.com",       "https://www.thedinkpickleball.com",    authority_score=75),
    SiteConfig("pickleballunion.com",         "https://pickleballunion.com",          authority_score=60),
    SiteConfig("pickleballmagazine.com",      "https://www.pickleballmagazine.com",   authority_score=70),
    SiteConfig("usapickleball.org",           "https://usapickleball.org",
               html_path="/category/news/",
               rss_paths=["/category/news/feed", "/feed", "/rss"],
               authority_score=90),
    SiteConfig("ppatour.com",                 "https://www.ppatour.com",
               html_path="/news/", authority_score=85),
    SiteConfig("majorleaguepickleball.co",    "https://majorleaguepickleball.co",     authority_score=85),
    SiteConfig("theapp.global",               "https://www.theapp.global",            authority_score=80),
    SiteConfig("dupr.com",                    "https://www.dupr.com",
               html_path="/blog", authority_score=75),
    SiteConfig("pickleheads.com",             "https://www.pickleheads.com",
               html_path="/blog", authority_score=65),
    SiteConfig("pickleballcentral.com",       "https://pickleballcentral.com",
               html_path="/blog/", authority_score=60),
    SiteConfig("justpaddles.com",             "https://www.justpaddles.com",
               html_path="/blog/", authority_score=60),
    SiteConfig("thekitchenpickle.com",        "https://thekitchenpickle.com",         authority_score=65),
    SiteConfig("pickleballportal.com",        "https://www.pickleballportal.com",     authority_score=60),
    SiteConfig("pickleballstudio.com",        "https://pickleballstudio.com",         authority_score=55),
    SiteConfig("pickleballeffect.com",        "https://pickleballeffect.com",         authority_score=55),
    SiteConfig("selkirk.com",                 "https://www.selkirk.com",
               html_path="/pages/blog", authority_score=70),
    SiteConfig("worldpickleballmagazine.com", "https://worldpickleballmagazine.com",  authority_score=65),
    SiteConfig("pickleballnewsasia.com",      "https://pickleballnewsasia.com",       authority_score=50),
    SiteConfig("pickleballtoday.co",          "https://pickleballtoday.co",           authority_score=55),
]

SITE_AUTHORITY: dict[str, int] = {s.name: s.authority_score for s in SITES}

# ============================================================================ #
# Article data model                                                             #
# ============================================================================ #

@dataclass
class ArticleData:
    url: str
    source_site: str
    title: str
    excerpt: str = ""
    content_text: str = ""
    author: str = ""
    image_url: str = ""
    published_at: datetime | None = None
    # Mention detection
    is_joola_mention: bool = False
    joola_context: str = ""
    players_mentioned: list[str] = field(default_factory=list)
    competitors_mentioned: list[str] = field(default_factory=list)
    has_competitor_mention: bool = False
    # Classification
    sentiment: str = "informative"
    sentiment_score: float = 0.0
    article_type: str = "general"
    relevance_type: str = "Industry News"
    importance_score: float = 0.0
    suggested_action: str = "No action needed"
    # AI enrichment
    ai_summary: str = ""
    why_it_matters: str = ""
    # Metrics
    word_count: int = 0
    content_hash: str = ""

# ============================================================================ #
# Date helpers                                                                   #
# ============================================================================ #

def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        return parsedate_to_datetime(raw).replace(tzinfo=timezone.utc)
    except Exception:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y",
    ):
        try:
            dt = datetime.strptime(raw[:len(fmt) + 5], fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def _is_recent(dt: datetime | None) -> bool:
    if dt is None:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=CUTOFF_DAYS)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= cutoff


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_hash(url: str, title: str, published_at: datetime | None) -> str:
    raw = f"{url}|{title.strip().lower()}|{published_at.date().isoformat() if published_at else ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]

# ============================================================================ #
# Entity detection                                                               #
# ============================================================================ #

def _detect_mentions(text: str) -> tuple[bool, str, list[str]]:
    is_joola = bool(JOOLA_RE.search(text))
    joola_context = ""
    if is_joola:
        sentences = re.split(r'(?<=[.!?])\s+', text)
        joola_context = " ".join(s for s in sentences if JOOLA_RE.search(s))[:500]
    players: list[str] = [p for p, pat in PLAYER_RES.items() if pat.search(text)]
    return is_joola, joola_context, players


def _detect_competitors(text: str) -> list[str]:
    return [b for b, pat in COMPETITOR_RES.items() if pat.search(text)]

# ============================================================================ #
# Classification                                                                 #
# ============================================================================ #

def _detect_sentiment(text: str) -> tuple[str, float]:
    words = re.findall(r'\b\w+\b', text.lower())
    pos = sum(1 for w in words if w in POSITIVE_WORDS)
    neg = sum(1 for w in words if w in NEGATIVE_WORDS)
    risk_count = sum(1 for w in words if w in RISK_WORDS)

    if risk_count >= 2:
        score = round(-0.6 + min(risk_count * 0.05, 0.3), 2)
        return "risk", score

    total = pos + neg
    if total == 0:
        return "informative", 0.0
    score = round((pos - neg) / total, 2)
    if score > 0.25:
        return "positive", score
    if score < -0.1:
        return "negative", score
    if pos > 0 and neg > 0 and abs(score) <= 0.15:
        return "mixed", score
    return "informative", score


def _classify_article_type(title: str, excerpt: str, players: list[str]) -> str:
    text = (title + " " + excerpt).lower()
    if any(w in text for w in [
        "tournament", "championship", "open", "masters", "gold",
        "silver", "bronze", "finals", "semifinal", "bracket",
    ]):
        return "tournament"
    if any(w in text for w in [
        "paddle", "gear", "equipment", "review", "product", "apparel",
        "ball", "shoe", "bag", "accessory", "launch", "release",
    ]):
        return "product"
    if players:
        return "ambassador"
    if any(w in text for w in ["opinion", "editorial", "commentary", "perspective", "why"]):
        return "opinion"
    return "general"


def _classify_relevance_type(
    is_joola: bool,
    players: list[str],
    competitors: list[str],
    title: str,
    excerpt: str,
) -> str:
    text = (title + " " + excerpt).lower()
    if is_joola:
        if any(kw in text for kw in JOOLA_PRODUCT_KEYWORDS):
            return "Product/Brand News"
        if players and any(kw in text for kw in TOURNAMENT_KEYWORDS):
            return "Tournament/Performance News"
        return "Direct JOOLA News"
    if players:
        if any(kw in text for kw in TOURNAMENT_KEYWORDS):
            return "Tournament/Performance News"
        return "Sponsored Player News"
    if competitors:
        return "Competitive News"
    return "Industry News"


def _compute_importance(article: ArticleData, authority: int = 50) -> float:
    score = 0.0
    if article.is_joola_mention:
        score += 35
    score += min(len(article.players_mentioned) * 8, 25)
    if article.sentiment in ("negative", "risk"):
        score += 18
    elif article.sentiment == "positive":
        score += 8
    elif article.sentiment == "mixed":
        score += 5
    if article.article_type == "tournament":
        score += 10
    elif article.article_type == "product":
        score += 8
    elif article.article_type == "ambassador":
        score += 6
    if article.has_competitor_mention:
        score += 5
    score += (authority / 100) * 10
    if article.published_at:
        tz = article.published_at.tzinfo or timezone.utc
        days_old = (datetime.now(timezone.utc) - article.published_at.replace(tzinfo=tz)).days
        if days_old < 7:
            score += 7
        elif days_old < 30:
            score += 3
    return min(round(score, 1), 100.0)


def _classify_suggested_action(article: ArticleData) -> str:
    sent = article.sentiment
    rel = article.relevance_type
    if sent in ("negative", "risk"):
        return "Risk review"
    if rel == "Direct JOOLA News" and sent == "positive":
        return "Share with marketing"
    if rel == "Product/Brand News":
        return "Product feedback"
    if rel == "Tournament/Performance News" and sent == "positive":
        return "PR opportunity"
    if rel == "Competitive News":
        return "Monitor competitor"
    if rel == "Sponsored Player News" and sent == "positive":
        return "Sponsorship opportunity"
    if article.article_type in ("product", "opinion") or rel == "Industry News":
        return "Use for SEO/blog"
    return "No action needed"


def _enrich(article: ArticleData, authority: int = 50) -> ArticleData:
    full_text = f"{article.title} {article.excerpt} {article.content_text}"
    article.is_joola_mention, article.joola_context, article.players_mentioned = _detect_mentions(full_text)
    article.competitors_mentioned = _detect_competitors(full_text)
    article.has_competitor_mention = bool(article.competitors_mentioned)
    article.sentiment, article.sentiment_score = _detect_sentiment(full_text)
    article.article_type = _classify_article_type(article.title, article.excerpt, article.players_mentioned)
    article.relevance_type = _classify_relevance_type(
        article.is_joola_mention, article.players_mentioned,
        article.competitors_mentioned, article.title, article.excerpt,
    )
    article.importance_score = _compute_importance(article, authority)
    article.suggested_action = _classify_suggested_action(article)
    article.word_count = len(full_text.split())
    article.content_hash = _compute_hash(article.url, article.title, article.published_at)
    return article

# ============================================================================ #
# AI enrichment — only for JOOLA-relevant articles, skip if already done        #
# ============================================================================ #

async def _ai_enrich(article: ArticleData, existing_hash: str | None = None) -> None:
    """Call LLM to generate executive summary and why_it_matters.

    Skipped if:
    - Article is not JOOLA-related (no brand + no player mention)
    - content_hash matches existing record (article unchanged and already enriched)
    """
    if not (article.is_joola_mention or article.players_mentioned or article.has_competitor_mention):
        return
    # If hash unchanged and caller says AI was already done, skip re-enrichment
    if existing_hash and existing_hash == article.content_hash:
        return

    try:
        from app.services.llm import chat_json
        from app.config import get_settings

        text_snippet = f"Title: {article.title}\n\nExcerpt: {article.excerpt[:600]}"
        if article.joola_context:
            text_snippet += f"\n\nJOOLA context: {article.joola_context[:300]}"

        players_str = ", ".join(article.players_mentioned[:5]) or "none"
        competitors_str = ", ".join(article.competitors_mentioned[:3]) or "none"

        result = await chat_json(
            system=(
                "You are a pickleball business intelligence analyst for JOOLA. "
                "Analyze news articles and return strict JSON with: "
                "executive_summary (2 sentences, factual), "
                "why_it_matters_for_joola (1-2 sentences, business relevance), "
                "suggested_action (must be one of: 'No action needed', "
                "'Share with marketing', 'Use for SEO/blog', 'Monitor competitor', "
                "'PR opportunity', 'Sponsorship opportunity', 'Product feedback', "
                "'Leadership review', 'Risk review')."
            ),
            user=(
                f"Source: {article.source_site}\n{text_snippet}\n\n"
                f"JOOLA players mentioned: {players_str}\n"
                f"Competitors mentioned: {competitors_str}\n"
                f"Sentiment: {article.sentiment} | Relevance: {article.relevance_type}"
            ),
            model=get_settings().openai_model_cheap,
            temperature=0.1,
        )

        article.ai_summary = (result.get("executive_summary") or "")[:500]
        article.why_it_matters = (result.get("why_it_matters_for_joola") or "")[:400]
        valid_actions = {
            "No action needed", "Share with marketing", "Use for SEO/blog",
            "Monitor competitor", "PR opportunity", "Sponsorship opportunity",
            "Product feedback", "Leadership review", "Risk review",
        }
        ai_action = result.get("suggested_action", "")
        if ai_action in valid_actions:
            article.suggested_action = ai_action

    except Exception as e:
        log.warning("ai_enrich_failed", url=article.url, error=str(e))

# ============================================================================ #
# RSS parsing                                                                    #
# ============================================================================ #

def _parse_rss_xml(xml_text: str, source: str, base_url: str) -> list[ArticleData]:
    try:
        soup = BeautifulSoup(xml_text, "lxml-xml")
    except Exception:
        soup = BeautifulSoup(xml_text, "html.parser")

    articles: list[ArticleData] = []
    items = soup.find_all("item") or soup.find_all("entry")

    for item in items:
        title_tag = item.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""
        if not title:
            continue

        link_tag = item.find("link")
        if link_tag:
            url = link_tag.get_text(strip=True) or link_tag.get("href", "")
        else:
            url = ""
        if not url or not url.startswith("http"):
            continue

        pub_tag = (item.find("pubDate") or item.find("published")
                   or item.find("updated") or item.find("dc:date"))
        published_at = _parse_date(pub_tag.get_text(strip=True) if pub_tag else None)
        if not _is_recent(published_at):
            continue

        desc_tag = (item.find("description") or item.find("summary")
                    or item.find("content:encoded"))
        excerpt = ""
        if desc_tag:
            raw = desc_tag.get_text(strip=True)
            excerpt_soup = BeautifulSoup(raw, "html.parser")
            excerpt = excerpt_soup.get_text(separator=" ", strip=True)[:600]

        image_url = ""
        media = item.find("media:content") or item.find("enclosure")
        if media:
            image_url = media.get("url", "")

        author_tag = item.find("author") or item.find("dc:creator")
        author = author_tag.get_text(strip=True) if author_tag else ""

        articles.append(ArticleData(
            url=url, source_site=source, title=title,
            excerpt=excerpt, author=author, image_url=image_url,
            published_at=published_at,
        ))

    return articles


async def _try_rss(client: httpx.AsyncClient, site: SiteConfig) -> list[ArticleData] | None:
    for path in site.rss_paths:
        url = site.base_url.rstrip("/") + path
        try:
            resp = await client.get(url, timeout=15)
            if resp.status_code != 200:
                continue
            ct = resp.headers.get("content-type", "")
            if not any(x in ct for x in ("xml", "rss", "atom", "text/")):
                continue
            articles = _parse_rss_xml(resp.text, site.name, site.base_url)
            if articles:
                log.info("rss_ok", site=site.name, path=path, count=len(articles))
                return articles
        except Exception:
            continue
    return None

# ============================================================================ #
# HTML listing scraper                                                           #
# ============================================================================ #

async def _scrape_html(client: httpx.AsyncClient, site: SiteConfig) -> list[ArticleData]:
    url = site.base_url.rstrip("/") + site.html_path
    try:
        resp = await client.get(url, timeout=20)
        if resp.status_code != 200:
            return []
    except Exception as e:
        log.warning("html_fetch_failed", site=site.name, url=url, error=str(e))
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    articles: list[ArticleData] = []
    seen_urls: set[str] = set()

    candidates = (
        soup.find_all("article")
        or soup.find_all(class_=re.compile(r"\b(post|news|article|blog|card|entry)\b", re.I))
    )
    if not candidates:
        candidates = soup.find_all("a", href=re.compile(r"/\d{4}/|\bpost\b|\barticle\b|\bnews\b|\bblog\b"))

    for container in candidates[:40]:
        a_tag = container if container.name == "a" else container.find("a", href=True)
        if not a_tag:
            continue
        href = a_tag.get("href", "")
        if not href:
            continue

        full_url = urljoin(site.base_url, href)
        if urlparse(full_url).netloc not in site.base_url:
            continue
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        h_tag = container.find(re.compile(r"h[123456]"))
        title = h_tag.get_text(strip=True) if h_tag else a_tag.get_text(strip=True)
        if not title or len(title) < 10:
            continue

        time_tag = container.find("time")
        date_str = (time_tag.get("datetime") or time_tag.get_text(strip=True)) if time_tag else None
        published_at = _parse_date(date_str)
        if not _is_recent(published_at):
            continue

        p_tags = container.find_all("p")
        excerpt = " ".join(p.get_text(strip=True) for p in p_tags[:2])[:500]

        img = container.find("img")
        image_url = (img.get("src") or img.get("data-src") or "") if img else ""
        if image_url and not image_url.startswith("http"):
            image_url = urljoin(site.base_url, image_url)

        articles.append(ArticleData(
            url=full_url, source_site=site.name, title=title,
            excerpt=excerpt, image_url=image_url, published_at=published_at,
        ))

    return articles

# ============================================================================ #
# Full content fetcher                                                           #
# ============================================================================ #

async def _fetch_full_content(client: httpx.AsyncClient, url: str) -> str:
    try:
        resp = await client.get(url, timeout=20)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["nav", "footer", "aside", "script", "style", "header"]):
            tag.decompose()
        content = (
            soup.find("article")
            or soup.find(class_=re.compile(r"\b(post-content|entry-content|article-body|content|body)\b", re.I))
            or soup.find("main")
        )
        text = content.get_text(separator=" ", strip=True) if content else soup.get_text(separator=" ", strip=True)
        return text[:3000]
    except Exception:
        return ""

# ============================================================================ #
# DB helpers                                                                     #
# ============================================================================ #

def _get_existing_hashes(db: Any, urls: list[str]) -> dict[str, dict]:
    """Batch-fetch existing articles' content_hash + ai_summary presence."""
    if not urls:
        return {}
    try:
        res = (
            db.table("news_articles")
            .select("url, content_hash, ai_summary")
            .in_("url", urls[:200])
            .execute()
        )
        return {r["url"]: r for r in (res.data or [])}
    except Exception:
        return {}


def _upsert_article(db: Any, article: ArticleData) -> bool:
    row = {
        "url": article.url,
        "source_site": article.source_site,
        "title": article.title,
        "excerpt": article.excerpt,
        "content_text": article.content_text,
        "author": article.author,
        "image_url": article.image_url,
        "published_at": article.published_at.isoformat() if article.published_at else None,
        "is_joola_mention": article.is_joola_mention,
        "joola_context": article.joola_context,
        "players_mentioned": article.players_mentioned,
        "competitors_mentioned": article.competitors_mentioned,
        "has_competitor_mention": article.has_competitor_mention,
        "sentiment": article.sentiment,
        "sentiment_score": article.sentiment_score,
        "article_type": article.article_type,
        "relevance_type": article.relevance_type,
        "importance_score": article.importance_score,
        "suggested_action": article.suggested_action,
        "word_count": article.word_count,
        "content_hash": article.content_hash,
        "scraped_at": _utcnow(),
        "is_active": True,
    }
    # Only include AI fields if populated (don't overwrite existing with empty)
    if article.ai_summary:
        row["ai_summary"] = article.ai_summary
    if article.why_it_matters:
        row["why_it_matters"] = article.why_it_matters

    try:
        db.table("news_articles").upsert(row, on_conflict="url").execute()
        return True
    except Exception as e:
        log.warning("upsert_failed", url=article.url, error=str(e))
        return False


def _store_error(db: Any, run_id: str, site_name: str, error_msg: str,
                 url: str = "", error_type: str = "scrape_error", status_code: int | None = None) -> None:
    try:
        db.table("news_scrape_errors").insert({
            "scrape_run_id": run_id,
            "source_name": site_name,
            "url": url,
            "error_type": error_type,
            "error_message": error_msg[:500],
            "status_code": status_code,
        }).execute()
    except Exception:
        pass

# ============================================================================ #
# Main orchestrator                                                              #
# ============================================================================ #

async def scrape_all_sites(run_id: str) -> None:
    """Entry point — called as FastAPI background task."""
    db = service_client()
    log_ctx = log.bind(run_id=run_id)

    db.table("news_scrape_runs").update({
        "status": "running",
        "started_at": _utcnow(),
    }).eq("id", run_id).execute()
    event_bus.publish(run_id, "start", message="News scraper started", sites_total=len(SITES))

    articles_found = 0
    articles_new = 0
    articles_mentions = 0
    articles_joola_related = 0
    successful_sources = 0
    failed_sources = 0

    timeout = httpx.Timeout(30.0, connect=10.0)
    headers = {"User-Agent": USER_AGENT}

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        for i, site in enumerate(SITES):
            site_failed = False
            try:
                event_bus.publish(run_id, "site", site=site.name, status="running", index=i)
                log_ctx.info("scraping_site", site=site.name)

                # Strategy 1: RSS; Strategy 2: HTML
                raw_articles = await _try_rss(client, site)
                if raw_articles is None:
                    raw_articles = await _scrape_html(client, site)

                # Batch pre-fetch existing records for dedup check
                urls = [a.url for a in raw_articles]
                existing = _get_existing_hashes(db, urls)

                site_new = 0
                for article in raw_articles:
                    # Fetch full content only for potentially relevant articles
                    quick_text = f"{article.title} {article.excerpt}"
                    needs_full = JOOLA_RE.search(quick_text) or any(
                        pat.search(quick_text) for pat in PLAYER_RES.values()
                    ) or any(
                        pat.search(quick_text) for pat in COMPETITOR_RES.values()
                    )
                    if needs_full and article.url:
                        article.content_text = await _fetch_full_content(client, article.url)
                        await asyncio.sleep(0.5)

                    article = _enrich(article, authority=site.authority_score)
                    articles_found += 1

                    is_joola_related = (
                        article.is_joola_mention
                        or bool(article.players_mentioned)
                        or article.has_competitor_mention
                    )
                    if is_joola_related:
                        articles_joola_related += 1
                    if article.is_joola_mention or article.players_mentioned:
                        articles_mentions += 1

                    # AI enrichment — skip if hash unchanged (unchanged article already enriched)
                    existing_rec = existing.get(article.url, {})
                    existing_hash = existing_rec.get("content_hash") if existing_rec else None
                    already_has_ai = bool(existing_rec.get("ai_summary")) if existing_rec else False
                    if is_joola_related and not (already_has_ai and existing_hash == article.content_hash):
                        await _ai_enrich(article, existing_hash=existing_hash if already_has_ai else None)

                    if not is_joola_related:
                        continue

                    if _upsert_article(db, article):
                        site_new += 1
                        articles_new += 1

                successful_sources += 1
                event_bus.publish(
                    run_id, "site",
                    site=site.name, status="done",
                    count=len(raw_articles), new=site_new,
                )

                # Update news_sources health
                try:
                    db.table("news_sources").update({
                        "last_success_at": _utcnow(),
                        "last_error": None,
                    }).eq("name", site.name).execute()
                except Exception:
                    pass

            except Exception as exc:
                failed_sources += 1
                site_failed = True
                err_str = str(exc)
                log_ctx.warning("site_failed", site=site.name, error=err_str)
                event_bus.publish(run_id, "site", site=site.name, status="error", error=err_str)
                _store_error(db, run_id, site.name, err_str, error_type="site_error")

                try:
                    db.table("news_sources").update({
                        "last_failed_at": _utcnow(),
                        "last_error": err_str[:255],
                    }).eq("name", site.name).execute()
                except Exception:
                    pass

            # Progress update after each site
            db.table("news_scrape_runs").update({
                "sites_scraped": i + 1,
                "articles_found": articles_found,
                "articles_new": articles_new,
                "articles_with_mentions": articles_mentions,
                "joola_related_articles": articles_joola_related,
                "successful_sources": successful_sources,
                "failed_sources": failed_sources,
            }).eq("id", run_id).execute()

            await asyncio.sleep(1.0)

    db.table("news_scrape_runs").update({
        "status": "done",
        "finished_at": _utcnow(),
        "articles_found": articles_found,
        "articles_new": articles_new,
        "articles_with_mentions": articles_mentions,
        "joola_related_articles": articles_joola_related,
        "successful_sources": successful_sources,
        "failed_sources": failed_sources,
    }).eq("id", run_id).execute()

    event_bus.publish(
        run_id, "done",
        articles_found=articles_found,
        articles_new=articles_new,
        articles_with_mentions=articles_mentions,
        joola_related=articles_joola_related,
        successful_sources=successful_sources,
        failed_sources=failed_sources,
    )
    log_ctx.info("scrape_complete", articles_found=articles_found, articles_new=articles_new)
