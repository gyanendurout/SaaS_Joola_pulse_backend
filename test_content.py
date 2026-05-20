import asyncio, httpx, json, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8003"

import uuid
RUN_ID = uuid.uuid4().hex[:8]

TESTS = [
    # (label, payload)
    # Tone: informative|hype|celebratory|defensive|educational|promotional
    # Audience: recreational|tournament|coaches|parents_juniors|general_fans|press_media
    # Length: short|medium|long
    # Unique created_by per test → fresh per-user rate-limit bucket per scenario
    ("TEST 1: IG Post — Athlete Win Celebration", {
        "content_type": "ig_post",
        "tone": "hype",
        "audience": "general_fans",
        "length": "medium",
        "instructions": "Celebrate Ben Johns winning the PPA Championship. Athlete-first tone.",
    }),
    ("TEST 2: Blog — Paddle Guide (short 800-900w)", {
        "content_type": "blog",
        "tone": "informative",
        "audience": "recreational",
        "length": "short",
        "instructions": "Write about choosing the right pickleball paddle for recreational players.",
    }),
    ("TEST 3: Blog — Tournament Recap (medium 1000-1200w)", {
        "content_type": "blog",
        "tone": "celebratory",
        "audience": "general_fans",
        "length": "medium",
        "instructions": "Write a tournament recap covering JOOLA athletes at the recent MLP season.",
    }),
    ("TEST 4: Twitter Crisis Response", {
        "content_type": "twitter_response",
        "tone": "defensive",
        "audience": "general_fans",
        "length": "medium",
        "instructions": "Respond to a viral tweet claiming JOOLA paddles are delaminating after 3 months.",
    }),
    ("TEST 5: IG Post — Product Launch Teaser", {
        "content_type": "ig_post",
        "tone": "hype",
        "audience": "tournament",
        "length": "medium",
        "instructions": "Tease the upcoming JOOLA Hyperion 2 paddle launch. No specs yet — just build excitement.",
    }),
    ("TEST 6: Blog — SEO Gap Fill (long 1300-1400w)", {
        "content_type": "blog",
        "tone": "educational",
        "audience": "recreational",
        "length": "long",
        "instructions": "Write a beginner guide to pickleball scoring rules for joola.com SEO.",
    }),
    ("TEST 7: IG Post — News Article Source (source_article_id)", {
        "content_type": "ig_post",
        "tone": "informative",
        "audience": "general_fans",
        "length": "medium",
        "instructions": "React to this JOOLA-related news article. Frame JOOLA's role positively.",
        "source_article_id": "d728f73c-f330-49bb-b045-cf7403c24ce4",
        "signals_config": {"news": True, "seo_keywords": False, "top_posts": True, "reddit": False},
    }),
    ("TEST 8: Twitter — Sport News Tie-in", {
        "content_type": "twitter_response",
        "tone": "informative",
        "audience": "press_media",
        "length": "medium",
        "instructions": "Share JOOLA perspective on the growth of professional pickleball leagues.",
        "signals_config": {"news": True, "seo_keywords": False, "top_posts": False, "reddit": True},
    }),
]

async def stream_gen(label, payload):
    print(f"\n{'='*60}")
    print(f"{label}")
    print(f"{'='*60}")
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            async with c.stream("POST", f"{BASE}/api/content/generate/stream", json=payload) as r:
                meta = parsed = critic = done_ev = None
                tokens = 0
                error_ev = None
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except Exception:
                        continue
                    t = ev.get("type")
                    if t == "meta":
                        meta = ev
                        print(f"  [meta] model={ev.get('model')} content_type={ev.get('content_type')}")
                    elif t == "token":
                        tokens += 1
                    elif t == "parsed":
                        parsed = ev
                    elif t == "critic":
                        critic = ev
                    elif t == "done":
                        done_ev = ev
                    elif t == "error":
                        error_ev = ev

                if error_ev:
                    print(f"  [ERROR] {error_ev}")
                    return None

                draft_id = done_ev.get("draft_id") if done_ev else None
                print(f"  tokens_streamed : {tokens}")
                print(f"  draft_id        : {draft_id}")

                if parsed:
                    vld = parsed.get("valid")
                    reasons = parsed.get("reasons", [])
                    inner = parsed.get("parsed") or {}
                    wc = inner.get("word_count")
                    tags = inner.get("hashtags")
                    reply_len = len(inner.get("reply") or "") if inner.get("reply") else None
                    snippet = ""
                    if inner.get("hook"):   snippet = inner["hook"][:70]
                    elif inner.get("title"): snippet = inner["title"][:70]
                    elif inner.get("reply"): snippet = inner["reply"][:70]
                    print(f"  parser valid    : {vld}  word_count={wc}  hashtags={len(tags) if tags is not None else 'n/a'}  reply_chars={reply_len}  reasons={reasons}")
                    print(f"  inner keys      : {list(inner.keys())}")
                    if snippet:
                        print(f"  content snippet : {snippet[:60]!r}")

                if critic:
                    passed = critic.get("passed")
                    viol = critic.get("violations", [])
                    fix = (critic.get("suggested_fix") or "")[:80]
                    print(f"  critic passed   : {passed}  violations={viol}")
                    if fix:
                        print(f"  critic fix hint : {fix}")

                if done_ev:
                    cost = done_ev.get("cost_usd", 0)
                    model = done_ev.get("model", "?")
                    latency = done_ev.get("latency_ms", "?")
                    print(f"  cost            : ${cost:.5f}  model={model}  latency={latency}ms")

                return draft_id
    except Exception as e:
        print(f"  [EXCEPTION] {e}")
        return None


async def main():
    draft_ids = []
    for i, (label, payload) in enumerate(TESTS, start=1):
        # Unique user per test to dodge the per-user 20/hr rate limit
        payload = {**payload, "created_by": f"qa-{RUN_ID}-t{i}@joola.com"}
        did = await stream_gen(label, payload)
        if did:
            draft_ids.append(did)

    # Check drafts listing
    print(f"\n{'='*60}")
    print("DRAFT PERSISTENCE CHECK")
    print(f"{'='*60}")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/api/content/drafts")
        data = r.json()
        total = data.get("total", 0)
        drafts = data.get("drafts", [])
        print(f"  Total drafts in DB: {total}")
        for d in drafts[:5]:
            print(f"    - {d.get('id')} | {d.get('content_type')} | {d.get('status')} | {str(d.get('body',''))[:60]}...")

    # Check usage
    print(f"\n{'='*60}")
    print("RATE LIMITER CHECK")
    print(f"{'='*60}")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/api/content/usage")
        u = r.json()
        print(f"  user_used_last_hour : {u.get('user_used_last_hour')}/{u.get('user_limit_per_hour')}")
        print(f"  org_used_today      : {u.get('org_used_today')}/{u.get('org_limit_per_day')}")
        print(f"  org_month_cost      : ${u.get('org_month_cost_usd',0):.4f} / ${u.get('org_month_cost_cap_usd',0)}")

    print(f"\n{'='*60}")
    print("TEMPLATES CHECK")
    print(f"{'='*60}")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/api/content/templates")
        templates = r.json()
        for t in templates:
            print(f"  [{t.get('content_type'):16s}] {t.get('name')}")

    print(f"\n{'='*60}")
    print(f"DONE — {len(draft_ids)} draft_ids returned")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
