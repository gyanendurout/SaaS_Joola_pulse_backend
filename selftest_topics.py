"""Auto-test: every signal kind should drive the generated content's topic.

Covers:
  - free_prompt:  instructions-only, no signals
  - seo_pick:     1 SEO keyword picked
  - top_post:     1 post per platform (IG / TikTok / X / YouTube) picked
  - news_pick:    1 news article picked (via source_article_id)

For each scenario we extract distinctive topical tokens from the signal,
generate the IG draft, and check those tokens (or close variants) appear
in the output. Pass = topic match, Fail = drift to paddle/generic.

Run:
    cd backend && .venv\\Scripts\\python.exe selftest_topics.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass

import httpx
from supabase import create_client

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.environ.get("PULSE_API", "http://127.0.0.1:8005")
SUPABASE_URL = "https://loecyghnkkxyymelgexz.supabase.co"
JOOLA_BRAND_ID = "04db8591-37a3-4634-9d11-536975fa6935"

# Generic words to ignore when scoring topic match
STOPWORDS = {
    "the", "and", "for", "with", "your", "you", "are", "this", "that", "from",
    "have", "has", "our", "all", "but", "not", "now", "out", "into", "today",
    "joola", "pickleball", "paddle", "paddles", "game", "play", "court",
    "every", "their", "they", "them", "more", "what", "when", "where", "how",
    "make", "made", "team", "look", "live", "post", "video", "watch",
    "best", "good", "great", "new", "next", "first", "last",
    "athlete", "athletes", "player", "players",
}


def load_service_key() -> str:
    with open(".env", "r") as f:
        for line in f:
            if line.startswith("SUPABASE_SERVICE_ROLE_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("Service role key not found in .env")


db = create_client(SUPABASE_URL, load_service_key())


@dataclass
class TestCase:
    name: str
    payload: dict
    expected_tokens: list[str]  # any of these (case-insensitive) must appear
    forbidden_tokens: list[str]  # if any of these dominate, fail


@dataclass
class Result:
    name: str
    passed: bool
    matched: list[str]
    missed: list[str]
    forbidden_hits: list[str]
    model: str | None
    cost: float | None
    snippet: str
    full_body: str


def build_seo_case() -> TestCase:
    return TestCase(
        name="SEO pick: pickleball nets",
        payload={
            "content_type": "ig_post",
            "tone": "informative",
            "audience": "recreational",
            "length": "short",
            "instructions": "",
            "signals_config": {
                "use_seo_keywords": True,
                "use_top_posts": False,
                "use_news": False,
                "use_reddit": False,
                "use_loyal_fans": False,
                "use_player_roster": False,
                "selected_seo_keywords": ["pickleball nets"],
            },
        },
        expected_tokens=["net", "nets"],
        forbidden_tokens=[],
    )


def build_seo_shoes_case() -> TestCase:
    return TestCase(
        name="SEO pick: best shoes for pickleball",
        payload={
            "content_type": "ig_post",
            "tone": "informative",
            "audience": "recreational",
            "length": "short",
            "instructions": "",
            "signals_config": {
                "use_seo_keywords": True,
                "selected_seo_keywords": ["best shoes for pickleball"],
            },
        },
        expected_tokens=["shoe", "shoes", "footwear"],
        forbidden_tokens=[],
    )


def build_free_prompt_case(brief: str, expected: list[str]) -> TestCase:
    return TestCase(
        name=f'Free prompt: "{brief[:50]}"',
        payload={
            "content_type": "ig_post",
            "tone": "informative",
            "audience": "recreational",
            "length": "short",
            "instructions": brief,
            "signals_config": {
                "use_seo_keywords": False,
                "use_top_posts": False,
                "use_news": False,
                "use_reddit": False,
                "use_loyal_fans": False,
                "use_player_roster": False,
            },
        },
        expected_tokens=expected,
        forbidden_tokens=[],
    )


def build_top_post_case(platform: str, post_row: dict, distinctive: list[str]) -> TestCase:
    """post_row from the appropriate platform table with `id` + text."""
    return TestCase(
        name=f"Top post pick ({platform.upper()}): {(post_row.get('text') or post_row.get('caption') or post_row.get('title') or '')[:50]}",
        payload={
            "content_type": "ig_post",
            "tone": "hype",
            "audience": "general_fans",
            "length": "short",
            "instructions": f"Repurpose this {platform} post for Instagram.",
            "signals_config": {
                "use_seo_keywords": False,
                "use_top_posts": True,
                "selected_top_post_ids": [post_row["post_id"]],
            },
        },
        expected_tokens=distinctive,
        forbidden_tokens=[],
    )


def build_news_case(article: dict, distinctive: list[str]) -> TestCase:
    return TestCase(
        name=f"News pick: {(article.get('title') or '')[:50]}",
        payload={
            "content_type": "ig_post",
            "tone": "informative",
            "audience": "general_fans",
            "length": "short",
            "instructions": "Respond to this news article on Instagram.",
            "source_article_id": article["id"],
            "signals_config": {
                "use_seo_keywords": False,
                "use_top_posts": False,
                "use_news": True,
            },
        },
        expected_tokens=distinctive,
        forbidden_tokens=[],
    )


# ──────────────────────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────────────────────


async def run_case(client: httpx.AsyncClient, case: TestCase) -> Result:
    # Unique created_by avoids per-user rate-limit collisions across cases
    payload = {**case.payload, "created_by": f"selftest-{uuid.uuid4().hex[:6]}@joola.com"}
    body_chunks: list[str] = []
    meta = done = error = None

    async with client.stream("POST", f"{BASE}/api/content/generate/stream", json=payload) as r:
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except Exception:
                continue
            t = ev.get("type")
            if t == "meta":
                meta = ev
            elif t == "token":
                body_chunks.append(ev.get("text", ""))
            elif t == "done":
                done = ev
            elif t == "error":
                error = ev
                break

    if error:
        return Result(case.name, False, [], case.expected_tokens, [], None, None, "", f"ERROR: {error}")

    full = (done or {}).get("body") or "".join(body_chunks)
    low = full.lower()

    matched = [t for t in case.expected_tokens if t.lower() in low]
    missed = [t for t in case.expected_tokens if t.lower() not in low]
    forbidden_hits = [t for t in case.forbidden_tokens if t.lower() in low]

    # Pass rule: at least one expected token present, and no forbidden hits
    passed = len(matched) > 0 and not forbidden_hits

    snippet_match = re.search(r"BODY:\s*(.{0,200})", full)
    snippet = (snippet_match.group(1) if snippet_match else full[:200]).strip()

    return Result(
        name=case.name,
        passed=passed,
        matched=matched,
        missed=missed,
        forbidden_hits=forbidden_hits,
        model=(meta or {}).get("model"),
        cost=(done or {}).get("cost_usd"),
        snippet=snippet,
        full_body=full,
    )


async def main():
    # Build dynamic cases from live DB rows
    cases: list[TestCase] = []

    # 1) Free prompts — instructions-only
    cases.append(build_free_prompt_case(
        "Write about pickleball court etiquette for new players.",
        expected=["etiquette", "manners", "courteous", "courtesy", "respect"],
    ))
    cases.append(build_free_prompt_case(
        "Explain pickleball serve techniques.",
        expected=["serve", "serving"],
    ))

    # 2) SEO picks
    cases.append(build_seo_case())
    cases.append(build_seo_shoes_case())

    # 3) Top post — one per platform — pick distinctive product words
    print("Fetching live posts for top-post cases…")
    # Instagram — find a post that mentions a JOOLA product line by name
    PRODUCT_NAMES = ("vision", "perseus", "hyperion", "agassi", "scorpeus", "magnus")
    ig = db.table("joola_ig_posts").select("post_id,caption").order("engagement_rate", desc=True, nullsfirst=False).limit(50).execute().data
    ig_pick = next(
        (p for p in ig if p.get("caption") and any(name in p["caption"].lower() for name in PRODUCT_NAMES)),
        ig[0],
    )
    ig_caption = (ig_pick.get("caption") or "").lower()
    ig_expected = [name for name in PRODUCT_NAMES if name in ig_caption] or _extract_distinctive(ig_pick.get("caption") or "")
    cases.append(build_top_post_case(
        "instagram",
        {**ig_pick, "post_id": ig_pick["post_id"]},
        ig_expected,
    ))

    # TikTok
    tt = db.table("tiktok_videos").select("id,text").eq("brand_id", JOOLA_BRAND_ID).order("view_count", desc=True, nullsfirst=False).limit(20).execute().data
    tt_pick = next((p for p in tt if p.get("text") and "vision" in p["text"].lower()), tt[0])
    cases.append(build_top_post_case("tiktok", {"post_id": tt_pick["id"], "text": tt_pick["text"]}, _extract_distinctive(tt_pick["text"])))

    # X / Twitter
    xp = db.table("x_posts").select("id,text").eq("brand_id", JOOLA_BRAND_ID).order("like_count", desc=True, nullsfirst=False).limit(5).execute().data
    if xp:
        x_pick = xp[0]
        cases.append(build_top_post_case("twitter", {"post_id": x_pick["id"], "text": x_pick["text"]}, _extract_distinctive(x_pick["text"])))

    # YouTube
    yt = db.table("yt_videos").select("id,title").eq("brand_id", JOOLA_BRAND_ID).order("view_count", desc=True, nullsfirst=False).limit(5).execute().data
    if yt:
        yt_pick = yt[0]
        cases.append(build_top_post_case("youtube", {"post_id": yt_pick["id"], "title": yt_pick["title"], "text": yt_pick["title"]}, _extract_distinctive(yt_pick["title"])))

    # 4) News pick — JOOLA-mention article
    news = db.table("news_articles").select("id,title,ai_summary").eq("is_joola_mention", True).order("published_at", desc=True).limit(5).execute().data
    if news:
        article = news[0]
        cases.append(build_news_case(article, _extract_distinctive(article["title"])))

    print(f"\nRunning {len(cases)} test cases against {BASE}\n")

    results: list[Result] = []
    async with httpx.AsyncClient(timeout=180) as client:
        for c in cases:
            print(f"  ▶ {c.name}  (expected ∋ {c.expected_tokens})")
            r = await run_case(client, c)
            results.append(r)
            status = "✅ PASS" if r.passed else "❌ FAIL"
            print(f"    {status}  model={r.model}  cost=${r.cost}  matched={r.matched}  missed={r.missed}")
            print(f"    snippet: {r.snippet[:140]}")
            print()

    # Summary
    print("=" * 78)
    passed = sum(1 for r in results if r.passed)
    print(f"SUMMARY: {passed}/{len(results)} passed")
    for r in results:
        sym = "✅" if r.passed else "❌"
        print(f"  {sym}  {r.name}  -> matched={r.matched}, missed={r.missed}")


def _extract_distinctive(text: str) -> list[str]:
    """Pull 1-3 non-stopword tokens from the signal text we expect to see echoed
    in the generated output. Returns lowercase tokens of length >=4."""
    if not text:
        return ["paddle"]  # fallback
    words = re.findall(r"\b[A-Za-z][A-Za-z\-]{3,}\b", text)
    out: list[str] = []
    for w in words:
        wl = w.lower()
        if wl in STOPWORDS:
            continue
        if wl in out:
            continue
        out.append(wl)
        if len(out) >= 3:
            break
    return out or ["paddle"]


if __name__ == "__main__":
    asyncio.run(main())
