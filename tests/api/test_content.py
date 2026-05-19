"""API tests for Content Studio endpoints.

Live HTTP tests are marked @pytest.mark.live and auto-skipped when uvicorn
isn't running at localhost:8000 (see backend/tests/conftest.py).

A handful of pure-unit tests at the bottom run unconditionally to verify the
parsers + builder + rate-limiter logic without needing the server or OpenAI.
"""
from __future__ import annotations

import asyncio

import pytest


# ─── Live HTTP tests (skipped without a running server) ──────────────────────

@pytest.mark.live
def test_drafts_list_returns_list(api):
    r = api.get("/api/content/drafts?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)
    assert "drafts" in body
    assert isinstance(body["drafts"], list)
    assert "total" in body


@pytest.mark.live
def test_drafts_get_404_on_unknown(api):
    r = api.get("/api/content/drafts/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


@pytest.mark.live
def test_signals_preview_shape(api):
    r = api.get("/api/content/signals/preview?content_type=ig_post")
    assert r.status_code == 200
    body = r.json()
    assert {"seo_keywords", "top_posts", "news", "reddit"}.issubset(body.keys())
    for k in ("seo_keywords", "top_posts", "news", "reddit"):
        assert isinstance(body[k], list)


@pytest.mark.live
def test_templates_list(api):
    r = api.get("/api/content/templates")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.live
def test_templates_list_filtered(api):
    r = api.get("/api/content/templates?content_type=ig_post")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.live
def test_generate_validates_body(api):
    # Empty body should 422 (missing content_type)
    r = api.post("/api/content/generate", json={})
    assert r.status_code in (400, 422)


@pytest.mark.live
def test_generate_stream_endpoint_reachable(api):
    r = api.post(
        "/api/content/generate/stream",
        json={
            "content_type": "ig_post",
            "tone": "informative",
            "audience": "general_fans",
            "length": "medium",
            "cta_goal": "none",
            "signals_config": {
                "seo_keywords": False,
                "top_posts": False,
                "news": False,
                "reddit": False,
                "loyal_fans": False,
                "player_roster": False,
            },
            "instructions": "smoke test",
        },
    )
    assert r.status_code != 404


@pytest.mark.live
def test_usage_endpoint(api):
    r = api.get("/api/content/usage")
    assert r.status_code == 200
    body = r.json()
    assert "user_limit_per_hour" in body
    assert "org_limit_per_day" in body


# ─── Unit tests (always run; no server / no OpenAI key needed) ───────────────

@pytest.mark.parametrize(
    "text,expected_hashtag_count,expected_mentions",
    [
        (
            "HOOK: Energy on day one.\n"
            "BODY: Para one of body text here describing the moment.\n\n"
            "Para two with more context and a real story.\n"
            "CTA: Tap the link in bio.\n"
            "HASHTAGS: #joola #pickleball #power #tournament #benjohns\n"
            "MENTIONS: @benjohns @joolausa",
            5,
            ["@benjohns", "@joolausa"],
        ),
        (
            "HOOK: Short hook.\n"
            "BODY: Body.\n"
            "CTA: Go.\n"
            "HASHTAGS: #a #b #c #d #e #f\n"
            "MENTIONS: none",
            6,
            [],
        ),
    ],
)
def test_parse_ig(text, expected_hashtag_count, expected_mentions):
    from app.content.parsers import parse_ig

    parsed = parse_ig(text)
    assert parsed["hook"]
    assert len(parsed["hashtags"]) == expected_hashtag_count
    assert parsed["mentions"] == expected_mentions


def test_parse_blog_extracts_title_and_meta():
    from app.content.parsers import parse_blog

    md = (
        "# How to Pick the Right JOOLA Paddle\n"
        "*Meta description (150-160 chars):* JOOLA paddles for power players, "
        "from Ben Johns to weekend warriors. Find the right balance of "
        "control and pop in 90 seconds.\n"
        "*Suggested hero alt-text:* Player swinging a JOOLA paddle.\n\n"
        "Intro hook here.\n\n"
        "## H2 One\nBody.\n\n"
        "## Final word\nWrap.\n"
    )
    parsed = parse_blog(md)
    assert parsed["title"] is not None
    assert "JOOLA" in parsed["title"]
    assert parsed["meta_description"] is not None
    assert parsed["alt_text"] is not None


def test_parse_twitter_splits_reply_and_alternate():
    from app.content.parsers import parse_twitter

    parsed = parse_twitter("REPLY: Thanks for the love.\nALTERNATE: We appreciate it.")
    assert parsed["reply"] == "Thanks for the love."
    assert parsed["alternate"] == "We appreciate it."


def test_validate_twitter_enforces_270_chars():
    from app.content.parsers import validate_twitter

    ok, reasons = validate_twitter({"reply": "x" * 300})
    assert not ok
    assert any("270" in r for r in reasons)


def test_prompt_builder_includes_verbatim_system_text():
    from app.content.prompt_builder import SHARED_SYSTEM_PROMPT

    # Anchors from spec §7.1 — must remain in the verbatim system prompt.
    assert "JOOLA Pulse content writer" in SHARED_SYSTEM_PROMPT
    assert "Ben Johns, Anna Leigh Waters, and Tyson McGuffin" in SHARED_SYSTEM_PROMPT
    assert "HARD RULES" in SHARED_SYSTEM_PROMPT


def test_rate_limiter_blocks_after_quota():
    from fastapi import HTTPException

    from app.content.rate_limiter import PER_USER_PER_HOUR, RateLimiter

    async def _run():
        rl = RateLimiter()
        for _ in range(PER_USER_PER_HOUR):
            await rl.check("user@x")
        with pytest.raises(HTTPException) as exc:
            await rl.check("user@x")
        assert exc.value.status_code == 429

    asyncio.run(_run())
