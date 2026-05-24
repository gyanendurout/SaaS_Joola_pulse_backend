# Backend Code Architecture

A map of `backend/` so you can find your way fast after restoration.

---

## Layout

```
backend/
├── app/
│   ├── main.py                     FastAPI app, CORS, route registration
│   ├── config.py                   Pydantic Settings from .env
│   ├── db.py                       Supabase client factory (service_client)
│   ├── orchestrator.py             Multi-agent SEO crawl orchestrator
│   │
│   ├── routes/                     FastAPI endpoint modules
│   │   ├── runs.py                 /api/runs — list/get SEO runs
│   │   ├── analyze.py              /api/analyze — start crawl (SSE)
│   │   ├── results.py              /api/results — query crawl results
│   │   ├── content.py              /api/content — generated content + calendar
│   │   ├── performance.py          /api/performance — GSC integration
│   │   ├── exports.py              /api/exports — CSV/XLSX exports
│   │   └── news.py                 /api/news/scrape, /events, /articles, /analytics/summary
│   │
│   ├── agents/                     SEO analysis agents
│   │   ├── crawl.py                Agent A — site crawl
│   │   ├── single_url.py           Agent — single-page analyze
│   │   ├── issues.py               Agent B — SEO issue detection (rules + LLM)
│   │   ├── entities.py             Agent C — entity extraction
│   │   ├── keywords.py             Agent D — keyword research (DataForSEO)
│   │   └── news_scraper.py         News pipeline (5 stages: fetch → entity → relevance → enrich → upsert)
│   │
│   ├── services/                   Lower-level helpers
│   │   ├── crawler.py              Crawl coordinator
│   │   ├── discovery.py            Sitemap / URL discovery
│   │   ├── parser.py               HTML → structured data
│   │   ├── page_classifier.py      Rule + LLM page-type classification
│   │   ├── playwright_fetcher.py   JS-rendered page fetch
│   │   ├── storage.py              File/blob storage abstraction
│   │   ├── llm.py                  OpenAI wrapper with cost cap
│   │   ├── dataforseo.py           DataForSEO API client
│   │   └── event_bus.py            SSE event bus for real-time progress
│   │
│   ├── models/
│   │   └── schemas.py              Pydantic request/response models
│   │
│   └── utils/
│
├── scripts/
│   ├── reset_db.py                 Helper: truncate runs cascade
│   └── test_dataforseo.py          Standalone DataForSEO connection test
│
├── supabase/
│   └── migrations/                 Source-of-truth SQL migrations (001-007)
│
├── recovery/                       This folder — disaster recovery package
├── qa/                             Regression scripts
├── scrape_now.py                   One-shot wrapper: trigger news scrape
├── run_scrape_bg.ps1               PowerShell: background news-scrape launcher
├── scrape_ig_latest.py             (Standalone) IG single-shot helper
├── requirements.txt
├── pyproject.toml
├── Procfile                        Railway start command
└── .env                            Local secrets (NOT committed)
```

---

## Key endpoints (FastAPI)

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/analyze` | Start an SEO crawl. Returns `{ run_id }`. Progress via SSE on `/api/events/{run_id}`. |
| GET | `/api/runs` | List SEO runs |
| GET | `/api/results/{run_id}` | Crawl results (pages, issues, entities, keywords) |
| POST | `/api/news/scrape` | Trigger news scrape (background task). Returns `{ run_id }`. |
| GET | `/api/news/scrape/{run_id}/events` | SSE progress stream |
| GET | `/api/news/articles` | Filtered article list |
| GET | `/api/news/analytics/summary` | KPI totals + trends |

The Next.js frontend hits these via the `/seo-api/*` rewrite (e.g. `fetch('/seo-api/news/articles')` → `${SEO_API_URL}/api/news/articles`).

---

## Where data flows (backend perspective)

```
                    ┌──────────────────────────────────────────┐
Apify Instagram ───►│ frontend/scripts/scrape_joola_ig.py      │──► Supabase joola_ig_*
                    │   (Python, lives in frontend repo)       │
                    └──────────────────────────────────────────┘

                    ┌──────────────────────────────────────────┐
20 pickleball   ───►│ backend/app/agents/news_scraper.py       │──► Supabase news_*
sites (RSS/HTML)    └──────────────────────────────────────────┘

                    ┌──────────────────────────────────────────┐
DataForSEO      ───►│ backend/app/services/dataforseo.py +     │──► Supabase keywords,
                    │ agents/keywords.py                       │     serp_results,
                    └──────────────────────────────────────────┘     domain_ranked_keywords

                    ┌──────────────────────────────────────────┐
Crawled HTML    ───►│ backend/app/services/crawler.py +        │──► Supabase pages,
                    │ agents/issues.py + entities.py           │     issues, entities
                    └──────────────────────────────────────────┘

[ Unknown source ]  ─?────────────────────────────────────────────► Supabase yt_*, tiktok_*,
                                                                     x_*, reddit_mentions,
                                                                     influencer_posts
```

Dashed-line tables are populated by scrapers not in this repo. See `scrapers/` for evidence and rebuild outlines.

---

## Coding conventions

1. **Pydantic Settings** loads `.env` once at startup via `app/config.py`. Never read `os.environ` directly elsewhere.
2. **All DB writes via `app/db.py`** — single supabase client factory using the service-role key.
3. **Agents are pure-ish** — each agent in `app/agents/` takes inputs and returns outputs; persistence happens at the route/orchestrator layer.
4. **SSE events go through `event_bus.py`** — never `print()` for progress, always emit events.
5. **LLM calls go through `services/llm.py`** — has cost cap + retry. Don't call OpenAI directly elsewhere.
6. **Pages discovered via `services/discovery.py`** — sitemap-first, fallback to crawl. Don't reinvent URL collection.

---

## Quick navigation

| To find... | Look at... |
|---|---|
| Where an endpoint is defined | `app/routes/<module>.py` |
| How an SEO crawl runs end-to-end | `app/orchestrator.py` (stage order) → `app/agents/*.py` |
| News scrape pipeline | `app/agents/news_scraper.py` (5 stages documented in module docstring) |
| Supabase schema | `supabase/migrations/*.sql` (for SEO + News tables) + `recovery/database/schema-overview.md` (for the rest) |
| External-API client code | `app/services/dataforseo.py`, `app/services/llm.py` |
| Pydantic response shapes | `app/models/schemas.py` |
