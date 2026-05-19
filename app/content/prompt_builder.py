"""Prompt builder — assembles system + user prompts from a ContextBundle.

System prompt and user prompts copied verbatim from spec §7.
"""
from __future__ import annotations

from app.content.types import (
    ContentType,
    ContextBundle,
    GenerateRequest,
    NewsSignal,
    RedditSignal,
    TopPostSignal,
)


# ─── Verbatim spec §7.1 ───────────────────────────────────────────────────────

SHARED_SYSTEM_PROMPT = """You are the JOOLA Pulse content writer. JOOLA is a pickleball brand whose roster includes Ben Johns, Anna Leigh Waters, and Tyson McGuffin.

VOICE RULES (non-negotiable):
- Athlete-first. Real players, real wins. Gear is the tool, not the hero.
- Technically credible. Use real specs (core mm, face material, swing weight) only when provided in context. Never invent numbers.
- Inclusive of all skill levels. No gatekeeping language.
- Energetic but not hype-fraud. No unsubstantiated superlatives.

HARD RULES (refuse or rewrite if violated):
- Do NOT name competitors (Selkirk, Engage, Paddletek, etc.) in promotional content.
- Do NOT fabricate stats, quotes, tournament results, or athlete claims.
- Do NOT make medical or injury-prevention claims.
- Do NOT use aggressive/violent verbs ("crush", "destroy", "kill").
- Do NOT construct `"<athlete_name> said"` quotes unless the exact quote is in input context.

Before returning, run the self-check listed in the user prompt and revise if any check fails."""


# ─── Verbatim spec §7.2 ───────────────────────────────────────────────────────

BLOG_USER_PROMPT = """TASK: Write a blog post for joola.com.

INPUTS
- User brief: {user_brief}
- Primary SEO keyword: {primary_keyword}
- Secondary keywords (use 2-3 naturally, density <2%): {secondary_keywords}
- Tone: {tone}
- Audience: {audience}
- Reference top-performing posts: {top_posts_context}
- Relevant news context (optional): {news_article_context}

OUTPUT (markdown, exactly this structure):
# {{H1 with primary keyword in first 60 chars}}
*Meta description (150-160 chars):* ...
*Suggested hero alt-text:* ...

[80-word intro hook]

## {{H2 #1}}
...

(3-5 H2 sections, 150-250 words each)

## Final word
[Conclusion + 1 CTA line]

SELF-CHECK: 1. Word count 800-1400? 2. Primary keyword in H1/first-100-words/one-H2/meta? 3. Any forbidden patterns? 4. Any invented stats? 5. Tone matches {tone}? If any fail, revise."""


# ─── Verbatim spec §7.3 ───────────────────────────────────────────────────────

IG_USER_PROMPT = """TASK: Write one Instagram caption.

INPUTS
- Brief: {user_brief}  | Tone: {tone}  | Audience: {audience}
- Top posts in theme (mimic structural pattern, not wording): {top_posts_context}
- News/event tie-in (optional): {news_article_context}

OUTPUT (parser-friendly, exact labels):
HOOK: <one line, ≤12 words>
BODY: <2-4 short paragraphs, blank lines between, 60-180 words total>
CTA: <one line>
HASHTAGS: #tag1 #tag2 ... (5-10 tags: branded + athlete + broad + 1-2 niche)
MENTIONS: @handle1 @handle2 (or "none")

SELF-CHECK: 1. Total 80-220 words? 2. ≤2 emojis? 3. Hashtags 5-10? 4. Hook ≤12 words and scroll-stopping? 5. No forbidden patterns? If any fail, revise."""


# ─── Verbatim spec §7.4 ───────────────────────────────────────────────────────

TWITTER_USER_PROMPT = """TASK: Draft a Twitter/X reply.

INPUTS
- Original tweet/context: {source_tweet_or_article}
- Reply intent (tone): {tone}
- Audience: {audience}
- JOOLA position: {user_brief}

OUTPUT:
REPLY: <≤270 chars, single tweet>
ALTERNATE (always for Crisis tone, optional otherwise): <a second, more conservative version>

SELF-CHECK: 1. ≤270 chars? 2. No competitor names? 3. If crisis tone — factual, no defensiveness in voice? 4. Mentions correct handle? If any fail, revise."""


# ─── Compression helpers ─────────────────────────────────────────────────────

def compress_top_posts(posts: list[TopPostSignal]) -> str:
    """Returns multi-line context per spec §A4."""
    if not posts:
        return "none"
    lines: list[str] = []
    for p in posts[:6]:
        theme = p.content_theme or "untagged"
        er = f"{p.engagement_rate * 100:.1f}" if (p.engagement_rate and p.engagement_rate <= 1) else (
            f"{p.engagement_rate:.1f}" if p.engagement_rate is not None else "?"
        )
        hook = (p.caption_first_line or "").strip().replace("\n", " ")[:120]
        lines.append(f'- {theme} | ER {er}% | hook: "{hook}"')
    return "\n".join(lines)


def compress_news(article: NewsSignal | None) -> str:
    """Title + ai_summary + why_it_matters only — never full body."""
    if article is None:
        return "none"
    bits: list[str] = [f"Title: {article.title}"]
    if article.ai_summary:
        bits.append(f"Summary: {article.ai_summary}")
    if article.why_it_matters:
        bits.append(f"Why it matters: {article.why_it_matters}")
    if article.suggested_action:
        bits.append(f"Suggested action: {article.suggested_action}")
    if article.players_mentioned:
        bits.append(f"Players mentioned: {', '.join(article.players_mentioned)}")
    if article.sentiment:
        bits.append(f"Sentiment: {article.sentiment}")
    return " · ".join(bits)


def compress_news_list(items: list[NewsSignal], limit: int = 3) -> str:
    if not items:
        return "none"
    return "\n".join(f"- {compress_news(a)}" for a in items[:limit])


def compress_reddit(row: RedditSignal) -> str:
    bits = [f"r/{row.subreddit or 'unknown'}: {row.title}"]
    if row.is_crisis:
        bits.append("[CRISIS]")
    if row.topics:
        bits.append(f"topics={','.join(row.topics[:4])}")
    if row.sentiment:
        bits.append(f"sentiment={row.sentiment}")
    if row.excerpt:
        bits.append(f'"{row.excerpt}"')
    return " · ".join(bits)


def compress_reddit_list(items: list[RedditSignal], limit: int = 5) -> str:
    if not items:
        return "none"
    return "\n".join(f"- {compress_reddit(r)}" for r in items[:limit])


def _primary_keyword(bundle: ContextBundle) -> str:
    if not bundle.seo_keywords:
        return ""
    return bundle.seo_keywords[0].keyword


def _secondary_keywords(bundle: ContextBundle, n: int = 5) -> str:
    if len(bundle.seo_keywords) <= 1:
        return ""
    return ", ".join(k.keyword for k in bundle.seo_keywords[1 : 1 + n])


def _voice_addendum(bundle: ContextBundle) -> str:
    """Brand voice block appended to system prompt with concrete bans."""
    bv = bundle.brand_voice
    parts: list[str] = ["", "BRAND VOICE CONFIG (current):"]
    if bv.tone:
        parts.append(f"- Available tones: {', '.join(bv.tone)}")
    if bv.banned_words:
        parts.append(f"- Banned words: {', '.join(bv.banned_words)}")
    if bv.signature_phrases:
        parts.append(f"- Signature phrases (use sparingly): {', '.join(bv.signature_phrases)}")
    if bv.default_ctas:
        parts.append(f"- Default CTAs: {', '.join(bv.default_ctas)}")
    if bv.forbidden_patterns:
        parts.append("- Forbidden patterns:")
        for p in bv.forbidden_patterns:
            parts.append(f"  • {p}")
    return "\n".join(parts) if len(parts) > 2 else ""


def _user_brief(request: GenerateRequest, bundle: ContextBundle) -> str:
    """User brief — falls back to a seed if instructions empty."""
    if request.instructions:
        return request.instructions.strip()
    if bundle.focus_news_article:
        a = bundle.focus_news_article
        action = a.suggested_action or "respond"
        return f'Write a {request.tone} {request.content_type} responding to "{a.title}" — angle: {action}'
    if bundle.seo_keywords:
        return f"Write about {bundle.seo_keywords[0].keyword}"
    return "Write a JOOLA Pulse post."


# ─── Main builder ─────────────────────────────────────────────────────────────

def build_prompt(
    content_type: ContentType | str,
    bundle: ContextBundle,
    request: GenerateRequest,
) -> dict:
    """Returns {'system': str, 'user': str}."""
    ct = content_type.value if isinstance(content_type, ContentType) else str(content_type)

    system = SHARED_SYSTEM_PROMPT + _voice_addendum(bundle)

    user_brief = _user_brief(request, bundle)
    tone = request.tone.value if hasattr(request.tone, "value") else str(request.tone)
    audience = request.audience.value if hasattr(request.audience, "value") else str(request.audience)

    if ct == "blog":
        user = BLOG_USER_PROMPT.format(
            user_brief=user_brief,
            primary_keyword=_primary_keyword(bundle) or "(none provided)",
            secondary_keywords=_secondary_keywords(bundle) or "(none provided)",
            tone=tone,
            audience=audience,
            top_posts_context=compress_top_posts(bundle.top_posts),
            news_article_context=compress_news(bundle.focus_news_article)
            if bundle.focus_news_article
            else compress_news_list(bundle.news),
        )
    elif ct == "ig_post":
        user = IG_USER_PROMPT.format(
            user_brief=user_brief,
            tone=tone,
            audience=audience,
            top_posts_context=compress_top_posts(bundle.top_posts),
            news_article_context=compress_news(bundle.focus_news_article)
            if bundle.focus_news_article
            else compress_news_list(bundle.news),
        )
    elif ct == "twitter_response":
        # For Twitter, "source" can be the focus news article OR a Reddit row OR the brief itself
        if bundle.focus_news_article:
            src = compress_news(bundle.focus_news_article)
        elif bundle.reddit:
            src = compress_reddit_list(bundle.reddit, limit=1)
        else:
            src = user_brief
        user = TWITTER_USER_PROMPT.format(
            source_tweet_or_article=src,
            tone=tone,
            audience=audience,
            user_brief=user_brief,
        )
    else:
        raise ValueError(f"Unknown content_type: {ct}")

    return {"system": system, "user": user}
