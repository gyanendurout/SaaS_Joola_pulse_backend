# Scrapers overview

JOOLA Pulse ingests data from 6 platforms. Only **two scrapers are fully present in the repo**; the rest produced data that exists in the DB but whose source code is **not in this codebase**. Each file in this folder documents what we know per platform — including evidence for the missing ones.

| Platform | Code location | Status | Schedule (intended) |
|---|---|---|---|
| **Instagram** | `frontend/scripts/scrape_joola_ig.py` | Fully in repo, runs standalone | Manual / cron — no scheduler in repo |
| **News (20 pickleball sites)** | `backend/app/agents/news_scraper.py` + `/api/news/scrape` route | Fully in repo, runs in-process via FastAPI | Manual trigger via API |
| **YouTube** | NOT IN REPO | Data present in DB; source unknown | Unknown |
| **TikTok** | NOT IN REPO | Data present in DB (110 vids @joolapickleball) | Unknown — see `tiktok-scraper.md` |
| **X / Twitter** | NOT IN REPO | Data present in DB (12 posts @joolausa) | Unknown — see `x-twitter-scraper.md` |
| **Reddit** | NOT IN REPO | Data present (123 mentions) | Unknown — see `reddit-scraper.md` |
| **AI enrichment** | Partial — IG only inline; News inline. TikTok/X/Reddit/YouTube: no enrichment script exists. | Gap | See `ai-enrichment-pipeline.md` |

---

## Where scrapers run

Today none of the scrapers have a documented scheduler. The pattern is:

1. **Instagram** — invoked from PowerShell on a dev machine. State file at `frontend/scripts/scrape_joola_ig.state.json` tracks progress. Log file at `frontend/scripts/scrape_joola_ig.log`. Idempotent (upserts by PK).
2. **News** — backend exposes `POST /api/news/scrape` which spawns a background task and streams SSE progress events. There's also `backend/scrape_now.py` (one-shot) and `backend/run_scrape_bg.ps1` (PowerShell wrapper).
3. **Other platforms** — see individual files in this folder for evidence-based hypotheses.

---

## Required credentials per scraper

| Scraper | Needs |
|---|---|
| Instagram | `APIFY_API_TOKEN`, `OPENAI_API_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `NEXT_PUBLIC_SUPABASE_URL` (read from `frontend/.env.local`) |
| News | `OPENAI_API_KEY`, Supabase keys; no Apify needed (uses `httpx` directly against RSS / HTML) |
| YouTube/TikTok/X/Reddit | Unknown — most likely Apify if they exist on the Apify account, otherwise platform APIs |

---

## Re-running after a wipe

In order:

1. Apply migrations + seed `brands`, `influencers`, account tables (see `01-database/`).
2. `python frontend/scripts/scrape_joola_ig.py` — backfills all IG history.
3. `curl -X POST $BACKEND/api/news/scrape` — scrapes 20 news sites for last 180 days.
4. For YT / TikTok / X / Reddit: investigate Apify console at https://console.apify.com with user `7nuiUPNRN29ouzcO6`. If actors exist, re-trigger them. If not, build per the per-platform files in this folder.
5. Run AI enrichment for the four currently-null-enriched platforms — script does not exist; see `ai-enrichment-pipeline.md` for a rebuild outline.
