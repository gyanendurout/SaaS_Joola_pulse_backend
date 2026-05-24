# AI enrichment pipeline

## Current state

| Platform | Enrichment status | Where it lives |
|---|---|---|
| Instagram comments | ✅ Done — runs inline with each scrape | `frontend/scripts/scrape_joola_ig.py` (writes to `joola_ig_comment_analysis`, `joola_ig_post_analysis`, `joola_ig_loyal_users`, `joola_ig_complaint_log`, `joola_ig_wishlist_items`) |
| News articles | ✅ Done — runs inline as Stage 4 of the news scrape | `backend/app/agents/news_scraper.py` (writes `sentiment`, `relevance_type`, `importance_score`, `ai_summary`, `why_it_matters`) |
| Reddit mentions | ⚠️ Partial — `topics`, `is_crisis`, `is_opportunity` populated for 119/123; `sentiment` is null for **all 123** | Unknown script, not in repo |
| TikTok videos | ❌ Not run — all AI columns null on 110 rows | No script exists |
| X posts | ❌ Not run — all AI columns null on 12 rows | No script exists |
| YouTube videos / comments | ❌ Not run + comment scraper itself broken (0 JOOLA rows in `yt_comments`) | No script exists |

## Why this is a gap

CLAUDE.md priority list (post-resume task #1):
> **Run AI enrichment** on 110 TikTok videos + 12 X posts (sentiment/topics/crisis/opportunity all null today).

This script does not exist in the repo. The IG scraper inlines its enrichment in `scrape_joola_ig.py`, but no equivalent standalone enrichment job exists for the other platforms. **You will have to write it as part of the disaster recovery.**

## Reference: how Instagram enrichment works

The pattern (extracted from `scrape_joola_ig.py`):

1. Fetch all comments lacking analysis: `WHERE joola_ig_comment_analysis.comment_id IS NULL`.
2. For each, call OpenAI `gpt-4o-mini` with a system prompt that returns JSON with sentiment, topic, emotion, complaint flag, purchase-intent flag.
3. Batched ~10 comments per call to stay under the $5 cost cap.
4. Upsert results to `joola_ig_comment_analysis` keyed on `comment_id`.

## Outline of a unified enrichment job

Recommend a single Python module that handles all four platforms with a common interface. Save as `backend/app/agents/social_enrichment.py`.

```python
# pseudocode skeleton — adapt to your tooling
from openai import OpenAI
from app.db import service_client

ENRICHMENT_TARGETS = [
    {
        "table": "tiktok_videos",
        "text_col": "text",
        "filter": "enriched_at IS NULL",
    },
    {
        "table": "x_posts",
        "text_col": "text",
        "filter": "enriched_at IS NULL",
    },
    {
        "table": "reddit_mentions",
        "text_col": "content_text",
        "filter": "sentiment IS NULL",
    },
    {
        "table": "yt_comments",
        "text_col": "text",
        "filter": "sentiment IS NULL",
    },
]

SYSTEM_PROMPT = """
You are a brand-intelligence analyst for JOOLA, a pickleball brand.
Classify the social-media post / comment below. Return JSON with:
- sentiment: "positive" | "negative" | "neutral" | "mixed"
- sentiment_score: float in [-1, 1]
- topics: list of short tags (e.g. ["paddle-grip", "tournament-result", "customer-service"])
- brands_mentioned: list of brand names (JOOLA, Selkirk, Paddletek, Franklin, CRBN, Engage, Onix, Six Zero, Proton, Head, Wilson)
- players_mentioned: list of pickleball players named (Ben Johns, Anna Bright, Tyson McGuffin, ...)
- is_crisis: true if this could escalate to PR risk
- is_opportunity: true if purchase intent / ambassador signal / partnership lead
- purchase_intent_score: float in [0, 1]
- crisis_keywords: list of phrases that triggered the crisis flag (empty if not crisis)
"""

def enrich(table, text_col, filter_clause):
    rows = service_client().table(table).select("*").filter("", "", filter_clause).limit(50).execute()
    for r in rows.data:
        if not r[text_col]:
            continue
        resp = OpenAI().chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": r[text_col]},
            ],
            response_format={"type": "json_object"},
        )
        analysis = json.loads(resp.choices[0].message.content)
        service_client().table(table).update({
            **analysis,
            "enriched_at": datetime.utcnow().isoformat(),
        }).eq("id", r["id"]).execute()
```

Wire this to a FastAPI endpoint:

```python
# backend/app/routes/enrichment.py
@router.post("/api/enrichment/run")
async def run_enrichment(background: BackgroundTasks):
    background.add_task(enrich_all_platforms)
    return {"status": "started"}
```

## Cost estimation

- `gpt-4o-mini` is roughly $0.15 / 1M input tokens + $0.60 / 1M output tokens (May 2026 pricing).
- Average post = 30 input tokens + 200 output tokens.
- Per call: ~$0.000125. For 245 backlog rows (110 TT + 12 X + 123 Reddit), total ~$0.03.
- Negligible. The OpenAI cost cap of $5 per run in `.env` is more than enough.

## Known gaps in the spec above

- `yt_comments` schema is not documented anywhere in the frontend (no page reads it directly per audit findings). Before enriching, run `SELECT column_name FROM information_schema.columns WHERE table_name='yt_comments'` against the production DB to learn the real schema.
- `reddit_mentions.competitor_switch` / `switch_direction` are not in the system prompt above — add them per the Reddit page schema if you want to populate them.
- For Reddit, the existing partial enrichment (`topics` set on 119/123 rows) was done by a separate pipeline. Before re-running, decide whether to overwrite or skip those rows. Recommend: overwrite (since `sentiment` is still null, those rows need a second pass anyway).
