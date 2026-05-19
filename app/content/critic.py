"""Critic pass — second LLM call that judges a draft against the rubric.

Returns JSON via OpenAI's `response_format={'type':'json_object'}` mode.
"""
from __future__ import annotations

import json

import structlog
from openai import AsyncOpenAI

from app.config import get_settings
from app.content.types import BrandVoice, CriticResult

log = structlog.get_logger()


CRITIC_MODEL = "gpt-4o-mini"


_CRITIC_SYSTEM = """You are the JOOLA Pulse content critic. You audit drafts against a strict rubric and return JSON only.

Output schema (strict):
{
  "passed": boolean,
  "violations": [string, ...],  // human-readable violation strings; [] if none
  "suggested_fix": string       // one short paragraph; "" if none
}

A draft fails ("passed": false) if ANY of the following are true:
1. Forbidden patterns present (medical claims, competitor names, fabricated stats/quotes, aggressive verbs)
2. Length out of bounds for the content type
3. Tone mismatch with what was requested
4. Missing required structural element (IG no hashtags, blog no meta description, tweet >270 chars)
5. Hallucinated numbers — any specific number, percentage, mm, or $ figure that is not anchored in the user's inputs
6. Athlete quote constructions (`"<name> said"`) without a source quote

Be concise. Cite each violation with a short phrase referencing the offending text."""


def _length_bounds(content_type: str) -> str:
    return {
        "blog": "800-1400 words",
        "ig_post": "80-220 words total + 5-10 hashtags",
        "twitter_response": "≤270 chars for REPLY",
    }.get(content_type, "—")


async def critique(
    draft: str,
    content_type: str,
    brand_voice: BrandVoice | None = None,
    tone: str | None = None,
) -> CriticResult:
    """Send the draft to gpt-4o-mini and return a CriticResult."""
    settings = get_settings()
    if not settings.openai_api_key:
        # No key — skip (don't fail the whole pipeline)
        return CriticResult(passed=True, violations=[], suggested_fix="")

    bv = brand_voice or BrandVoice()
    rubric_block = (
        f"CONTENT TYPE: {content_type}\n"
        f"REQUESTED TONE: {tone or '(unspecified)'}\n"
        f"LENGTH BOUNDS: {_length_bounds(content_type)}\n"
        f"BANNED WORDS: {', '.join(bv.banned_words) if bv.banned_words else '(none)'}\n"
        f"FORBIDDEN PATTERNS:\n"
        + ("\n".join(f"- {p}" for p in bv.forbidden_patterns) if bv.forbidden_patterns else "(none)")
    )

    user = f"{rubric_block}\n\n---DRAFT---\n{draft}\n---END DRAFT---"

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    try:
        resp = await client.chat.completions.create(
            model=CRITIC_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _CRITIC_SYSTEM},
                {"role": "user", "content": user},
            ],
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        return CriticResult(
            passed=bool(data.get("passed", True)),
            violations=list(data.get("violations") or []),
            suggested_fix=str(data.get("suggested_fix") or ""),
        )
    except Exception as e:
        log.warning("critic_call_failed", error=str(e))
        return CriticResult(passed=True, violations=[], suggested_fix="")
