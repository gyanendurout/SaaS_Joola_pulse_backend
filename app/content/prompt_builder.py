"""Prompt builder — assembles system + user prompts from a ContextBundle.

System prompt and user prompts copied verbatim from spec §7.
"""
from __future__ import annotations

import re

from app.content.types import (
    ContentType,
    ContextBundle,
    GenerateRequest,
    NewsSignal,
    RedditSignal,
    TopPostSignal,
)


# Generic openers that mean "use the signal as the topic" — not real briefs.
_TEMPLATE_OPENERS = (
    "repurpose this",
    "respond to this",
    "write about this",
    "based on the",
    "remix this",
    "react to this",
)


def _brief_is_substantive(text: str) -> bool:
    """True when the user typed a brief that introduces its own topic.

    In that case the picked signal should be treated as STYLE reference only,
    not as the topic. Heuristic:
      - empty / very short  → not substantive (signal drives topic)
      - starts with a template opener ("repurpose this ...") → not substantive
      - contains a non-JOOLA capitalized proper noun (e.g. "Coca-Cola", "Razer")
        → substantive
      - 5+ non-trivial words → substantive
    """
    if not text:
        return False
    t = text.strip()
    if len(t) < 4:
        return False
    low = t.lower()
    if any(low.startswith(op) for op in _TEMPLATE_OPENERS):
        return False
    # Proper nouns / brands the user is bringing in
    proper = re.findall(r"\b[A-Z][a-zA-Z]+(?:[-\s][A-Z][a-zA-Z]+)*\b", t)
    proper = [w for w in proper if w.lower().replace(" ", "") not in {"joola"}]
    if proper:
        return True
    words = re.findall(r"\w+", t)
    return len(words) >= 5


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

BLOG_USER_PROMPT = """TASK: Write a blog post for joola.com. Target length: {word_target} words — you MUST hit this range.

INPUTS
- User brief: {user_brief}
- Primary SEO keyword: {primary_keyword}
- Secondary keywords (use 2-3 naturally, density <2%): {secondary_keywords}
- Tone: {tone}
- Audience: {audience}
- Reference post(s) — follow the TOPIC OVERRIDE or STYLE REFERENCE block above (if any). When no override block is present and ONE post is listed, expand on its product/topic; when multiple posts are listed, structural inspiration only: {top_posts_context}
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

SELF-CHECK: 1. Word count {word_target}? 2. Primary keyword in H1/first-100-words/one-H2/meta? 3. Any forbidden patterns? 4. Any invented stats? 5. Tone matches {tone}? If any fail, revise."""


# ─── Verbatim spec §7.3 ───────────────────────────────────────────────────────

IG_USER_PROMPT = """TASK: Write one Instagram caption with target length around {word_target} words.

INPUTS
- Brief: {user_brief}  | Tone: {tone}  | Audience: {audience}
- Required keywords (use at least 2 in the HOOK or BODY, natural phrasing): {seo_keywords_context}
- Reference post(s) — follow the TOPIC OVERRIDE or STYLE REFERENCE block above (if any). When no override block is present and ONE post is listed, treat it as the topical seed; when multiple posts are listed, treat them as pattern inspiration only: {top_posts_context}
- News/event tie-in (optional): {news_article_context}

OUTPUT (parser-friendly, exact labels):
HOOK: <one line, ≤12 words>
BODY: <2-4 short paragraphs, blank lines between, 60-180 words total>
CTA: <one line>
HASHTAGS: #tag1 #tag2 ... (5-10 tags: branded + athlete + broad + 1-2 niche)
MENTIONS: @handle1 @handle2 (or "none")

SELF-CHECK: 1. Total {word_target} words? 2. At least 2 of the required keywords appear verbatim in HOOK or BODY (if any were provided)? 3. ≤2 emojis? 4. Hashtags 5-10? 5. Hook ≤12 words and scroll-stopping? 6. No forbidden patterns? If any fail, revise."""


# ─── Verbatim spec §7.4 ───────────────────────────────────────────────────────

TWITTER_USER_PROMPT = """TASK: Draft a Twitter/X reply.

INPUTS
- Original tweet/context: {source_tweet_or_article}
- Reply intent (tone): {tone}
- Audience: {audience}
- JOOLA position: {user_brief}
- Required keywords (work at least 1 into the REPLY if relevant): {seo_keywords_context}

OUTPUT:
REPLY: <≤270 chars, single tweet>
ALTERNATE (always for Crisis tone, optional otherwise): <a second, more conservative version>

SELF-CHECK: 1. ≤270 chars? 2. No competitor names? 3. If crisis tone — factual, no defensiveness in voice? 4. Mentions correct handle? 5. At least 1 required keyword appears verbatim in REPLY (if any provided)? If any fail, revise."""


# ─── Compression helpers ─────────────────────────────────────────────────────

def compress_top_posts(posts: list[TopPostSignal]) -> str:
    """Multi-line top-post context. Includes platform, engagement, and the
    actual caption/title so the LLM can ground new copy in the picked post.

    When a single post is supplied (user-picked), the LLM should treat that
    post's hook + theme as a direct reference for tone and topic. With many
    posts, it's pattern inspiration only.
    """
    if not posts:
        return "none"
    lines: list[str] = []
    for p in posts[:10]:
        platform = getattr(p, "platform", None) or "instagram"
        theme = p.content_theme or "untagged"
        er = ""
        if p.engagement_rate is not None:
            er = f"ER {p.engagement_rate * 100:.1f}%" if p.engagement_rate <= 1 else f"ER {p.engagement_rate:.1f}%"
        # Platform-native primary engagement signal in addition to ER
        eng_extra = []
        if getattr(p, "views", None):
            eng_extra.append(f"{p.views} views")
        if getattr(p, "likes", None):
            eng_extra.append(f"{p.likes} likes")
        metrics = " · ".join(filter(None, [er, *eng_extra])) or "no metrics"
        hook = (p.caption_first_line or "").strip().replace("\n", " ")[:240]
        lines.append(f"- [{platform.upper()}] theme={theme} | {metrics} | text: \"{hook}\"")
    if len(posts) == 1:
        lines.append("(Single picked post — match its theme, vocabulary, and energy in the new draft.)")
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


def _has_picked_signal(bundle: ContextBundle) -> bool:
    """True when the user made a deliberate signal pick that should drive
    topic selection — used to suppress generic brand-voice signature phrases
    and to upgrade the model to gpt-4o for better instruction-following.
    """
    if len(bundle.top_posts) == 1:
        return True
    if 1 <= len(bundle.seo_keywords) <= 3:
        return True
    if bundle.focus_news_article is not None:
        return True
    return False


def _voice_addendum(bundle: ContextBundle) -> str:
    """Brand voice block appended to system prompt with concrete bans.

    Signature phrases are suppressed when the user picked ANY specific signal
    (single top post, 1-3 SEO keywords, or a focus news article). Those phrases
    otherwise drown out the picked signal's subject and the LLM defaults to
    generic paddle/athlete copy.
    """
    bv = bundle.brand_voice
    suppress_signatures = _has_picked_signal(bundle)
    parts: list[str] = ["", "BRAND VOICE CONFIG (current):"]
    if bv.tone:
        parts.append(f"- Available tones: {', '.join(bv.tone)}")
    if bv.banned_words:
        parts.append(f"- Banned words: {', '.join(bv.banned_words)}")
    if bv.signature_phrases and not suppress_signatures:
        parts.append(f"- Signature phrases (use sparingly): {', '.join(bv.signature_phrases)}")
    if bv.default_ctas:
        parts.append(f"- Default CTAs: {', '.join(bv.default_ctas)}")
    if bv.forbidden_patterns:
        parts.append("- Forbidden patterns:")
        for p in bv.forbidden_patterns:
            parts.append(f"  • {p}")
    return "\n".join(parts) if len(parts) > 2 else ""


def _user_brief(request: GenerateRequest, bundle: ContextBundle) -> str:
    """User brief — falls back to a seed if instructions empty.

    Two modes:
      A) Brief is substantive (introduces its own topic, e.g. "Coca-Cola X JOOLA"):
         picked signals become STYLE / TONE reference only. The brief is the topic.
      B) Brief is empty or just a template opener ("Repurpose this..."):
         picked signal drives the topic (TOPIC OVERRIDE).
    """
    raw_instructions = (request.instructions or "").strip()
    brief_topical = _brief_is_substantive(raw_instructions)

    base = raw_instructions
    if not base:
        if bundle.focus_news_article:
            a = bundle.focus_news_article
            action = a.suggested_action or "respond"
            base = f'Write a {request.tone} {request.content_type} responding to "{a.title}" — angle: {action}'
        elif bundle.seo_keywords:
            base = f"Write about {bundle.seo_keywords[0].keyword}"
        else:
            base = "Write a JOOLA Pulse post."

    override: str | None = None

    # Priority 1 — explicit top post pick (single)
    if len(bundle.top_posts) == 1:
        p = bundle.top_posts[0]
        platform = getattr(p, "platform", None) or "instagram"
        text = (p.caption_first_line or "").strip().replace("\n", " ")[:400]
        if text and brief_topical:
            # Mode A — brief is topic, post is style reference only.
            override = (
                "==== STYLE REFERENCE ONLY — READ THIS FIRST ====\n"
                f"The user picked a JOOLA {platform.upper()} post AS A STYLE REFERENCE, but their brief below is the TOPIC.\n"
                f'Style-reference post text: "{text}"\n\n'
                "RULES — non-negotiable:\n"
                "1. The TOPIC of your new draft is whatever the user brief below describes — NOT the picked post's subject.\n"
                "2. Use the picked post only for tone, structure, energy, and pacing.\n"
                "3. Do NOT mention the picked post's specific products, partners, athletes, or events unless the user brief also names them.\n"
                "4. If the picked post is about a brand collab (e.g. Razer × JOOLA) and the brief is about a different one (e.g. Coca-Cola × JOOLA), write about the brief's collab — never the post's.\n"
                "==== END STYLE REFERENCE ====\n\n"
            )
        elif text:
            # Mode B — brief is empty/template, post drives topic.
            override = (
                "==== TOPIC OVERRIDE — READ THIS FIRST ====\n"
                f"The user picked one specific JOOLA {platform.upper()} post and wants you to repurpose it.\n"
                f'Picked post text: "{text}"\n\n'
                "RULES — non-negotiable:\n"
                "1. Your new draft is ABOUT the exact subject of the picked post above (product name, event, athlete, paddle line).\n"
                "2. If the picked post names a product (e.g. 'JOOLA Vision Paddles', 'Perseus Pro IV', 'Hyperion'), use that exact product name verbatim in the HOOK or BODY.\n"
                "3. Do NOT replace specifics with brand signature phrases like 'real players, real wins' or 'power, control, precision' — those are background voice, not the topic.\n"
                "4. The picked post's energy/tone is your style reference; its subject is your subject.\n"
                "==== END TOPIC OVERRIDE ====\n\n"
            )

    # Priority 2 — explicit SEO keyword pick (no top post override above)
    elif bundle.seo_keywords and 1 <= len(bundle.seo_keywords) <= 3:
        kws = [k.keyword for k in bundle.seo_keywords]
        primary = kws[0]
        kw_list = ", ".join(f'"{k}"' for k in kws)
        if brief_topical:
            override = (
                "==== SECONDARY KEYWORDS ====\n"
                f"User picked SEO keyword(s) {kw_list} — weave them into the body where they fit naturally.\n"
                "The PRIMARY topic is the user brief below — do NOT replace it with the keywords.\n"
                "==== END SECONDARY KEYWORDS ====\n\n"
            )
        else:
            override = (
                "==== TOPIC OVERRIDE — READ THIS FIRST ====\n"
                f"The user picked specific SEO keyword(s): {kw_list}.\n\n"
                "RULES — non-negotiable:\n"
                f'1. The PRIMARY SUBJECT of your new draft is "{primary}" — write about THAT thing.\n'
                "2. If the keyword names a product category (e.g. 'pickleball nets', 'pickleball shoes', 'pickleball racket'), the draft must be about that product category — NOT about pickleball paddles unless 'paddle' is one of the picked keywords.\n"
                "3. Use every picked keyword verbatim at least once (HOOK or BODY).\n"
                "4. Do NOT default to JOOLA's flagship paddle copy unless the user picked a paddle keyword.\n"
                "==== END TOPIC OVERRIDE ====\n\n"
            )

    if override:
        base = override + base
    return base


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

    # Comma-separated list of every keyword the user picked (for IG / Twitter)
    all_kws = ", ".join(k.keyword for k in bundle.seo_keywords) or "(none provided)"

    if ct == "blog":
        length_val = request.length.value if hasattr(request.length, "value") else str(request.length)
        word_target = {"short": "800-900", "medium": "1000-1200", "long": "1300-1400"}.get(length_val, "1000-1200")
        user = BLOG_USER_PROMPT.format(
            word_target=word_target,
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
        length_val = request.length.value if hasattr(request.length, "value") else str(request.length)
        ig_word_target = {"short": "80-120", "medium": "140-180", "long": "200-220"}.get(length_val, "140-180")
        user = IG_USER_PROMPT.format(
            word_target=ig_word_target,
            user_brief=user_brief,
            tone=tone,
            audience=audience,
            seo_keywords_context=all_kws,
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
            seo_keywords_context=all_kws,
        )
    else:
        raise ValueError(f"Unknown content_type: {ct}")

    return {"system": system, "user": user}
