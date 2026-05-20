# SEO Intel — Backend
<!-- redeploy: 2026-05-20T21:01 -->


FastAPI service that crawls a URL, runs a 10-step SEO analysis pipeline, and exposes the results over a REST + Server-Sent Events (SSE) API.

---

## Tech stack

| Layer | Library |
|---|---|
| Web framework | FastAPI + Uvicorn |
| Data validation | Pydantic v2 + pydantic-settings |
| Database | Supabase (Postgres) via `supabase-py` |
| HTTP / crawling | httpx + BeautifulSoup4 + lxml |
| JS-render fallback | Playwright (Chromium) |
| Cloud crawler (optional) | Apify |
| LLM | OpenAI (`gpt-4o-mini` / `gpt-4o`) |
| Keyword / SERP data | DataForSEO API |
| Search Console | Google API Python client (optional) |
| Reports | openpyxl (Excel) |
| Logging | structlog (JSON) |

Python **3.11+** required.

---

## Project layout

```
backend/
├── app/
│   ├── main.py            ASGI entry point; mounts all routers
│   ├── config.py          Pydantic Settings; reads .env
│   ├── db.py              Supabase client singletons (anon + service-role)
│   ├── orchestrator.py    Sequential A→B→C→D pipeline runner
│   ├── agents/
│   │   ├── single_url.py  Main 10-step single-URL analysis
│   │   ├── crawl.py       Multi-page crawl agent (full-site mode)
│   │   ├── issues.py      Rule-based SEO issue detection
│   │   ├── entities.py    LLM entity extraction
│   │   └── keywords.py    DataForSEO keyword fetching
│   ├── routes/
│   │   ├── analyze.py     POST /api/analyze, SSE stream, dashboard
│   │   ├── runs.py        CRUD for analysis runs
│   │   ├── results.py     Pages, issues, entities, keywords endpoints
│   │   ├── exports.py     JSON / CSV / Excel / PDF exports
│   │   ├── content.py     AI content generation (blog, email, social)
│   │   └── performance.py Google Search Console integration
│   ├── services/
│   │   ├── crawler.py     HTTP fetcher with Playwright + Apify fallback
│   │   ├── parser.py      HTML → structured page data (title, meta, headings…)
│   │   ├── dataforseo.py  DataForSEO API client (keywords, SERP, backlinks)
│   │   ├── llm.py         OpenAI wrapper (chat_json helper)
│   │   ├── event_bus.py   In-process SSE event bus
│   │   ├── storage.py     Local filesystem storage for crawled HTML
│   │   ├── discovery.py   Site-map / link discovery
│   │   ├── page_classifier.py  URL → page type classifier
│   │   └── playwright_fetcher.py  JS-render fallback
│   ├── models/
│   │   └── schemas.py     Shared Pydantic request / response models
│   └── utils/
├── scripts/
│   ├── reset_db.py        Truncate all tables (dev helper)
│   └── test_dataforseo.py Smoke-test DataForSEO credentials
├── supabase/
│   └── migrations/        SQL files — apply manually in Supabase SQL editor
│       ├── 001_init.sql
│       ├── 002_single_url.sql
│       ├── 003_extended.sql
│       ├── 004_seed_keywords.sql
│       └── 005_content.sql
├── storage/               Runtime: crawled HTML snapshots (gitignored)
├── pyproject.toml
└── .env.example
```

---

## Analysis pipeline

Each analysis runs 10 sequential steps. Progress is streamed to the frontend over SSE in real time.

```
POST /api/analyze
        │
        ▼
 1. fetch             HTTP GET the target URL (Playwright/Apify fallback)
 2. parse             Extract title, meta, H1-H6, links, schema.org, images
 3. issues            Rule-based SEO checks (15+ rules — missing title, duplicate H1, etc.)
 4. entities          GPT-4o reads the page → products / categories / topics / brands
 5. keywords          DataForSEO Keyword Ideas seeded from entities
 6. serp              Top-10 Google results for leading keywords
 7. recommendations   GPT-4o → prioritised, actionable fixes
 8. ranked_keywords   Keywords the domain already ranks for (DataForSEO)
 9. competitors       Organic competitor domains (DataForSEO)
10. backlinks         Domain backlink profile (DataForSEO)
11. gap_analysis      Delta vs. previous run (only on re-analyses)
```

Results are persisted to Supabase and exposed via `/api/analyze/{id}/dashboard`.

---

## API reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Liveness check |
| `POST` | `/api/analyze` | Start a new analysis |
| `GET` | `/api/analyze` | List recent analyses |
| `GET` | `/api/analyze/{id}` | Get a single analysis run |
| `GET` | `/api/analyze/{id}/events` | SSE stream of live step progress |
| `GET` | `/api/analyze/{id}/dashboard` | Full dashboard payload (all tabs) |
| `PUT` | `/api/analyze/{id}/seed-keywords` | Override seed keywords for a domain |
| `GET` | `/api/runs/{id}/export.json` | Full JSON export |
| `GET` | `/api/runs/{id}/export.csv` | CSV zip of all tables |
| `GET` | `/api/runs/{id}/report.xlsx` | Excel workbook |
| `GET` | `/api/runs/{id}/report.pdf` | Print-ready PDF report |
| `POST` | `/api/runs/{id}/blog-ideas` | AI: generate blog post ideas |
| `POST` | `/api/runs/{id}/blog-outline` | AI: generate blog outline |
| `POST` | `/api/runs/{id}/blog-draft` | AI: generate full blog draft |
| `POST` | `/api/runs/{id}/email` | AI: generate email copy |
| `POST` | `/api/runs/{id}/social` | AI: generate social media captions |
| `POST` | `/api/runs/{id}/calendar` | AI: generate 4-week content calendar |
| `GET` | `/api/performance/status` | Google Search Console connection status |
| `GET` | `/api/performance/{domain}/gsc` | GSC clicks / impressions / CTR data |

Interactive docs (Swagger UI) at **http://localhost:8000/docs** when running locally.

---

## Setup

### 1. Database — Supabase

1. Create a new [Supabase](https://supabase.com) project.
2. In the SQL editor, run each migration file in order:
   ```
   supabase/migrations/001_init.sql
   supabase/migrations/002_single_url.sql
   supabase/migrations/003_extended.sql
   supabase/migrations/004_seed_keywords.sql
   supabase/migrations/005_content.sql
   ```
3. Copy your **Project URL**, **anon key**, and **service-role key** from *Project Settings → API*.

### 2. Python environment

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
playwright install chromium   # only needed for JS-rendered pages
```

### 3. Environment variables

```powershell
cp .env.example .env
# then open .env and fill in the values below
```

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | Yes | GPT-4o access required |
| `DATAFORSEO_LOGIN` | Yes | From app.dataforseo.com |
| `DATAFORSEO_PASSWORD` | Yes | |
| `SUPABASE_URL` | Yes | `https://<project>.supabase.co` |
| `SUPABASE_ANON_KEY` | Yes | Public anon key |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes | Server-side only — never expose |
| `SUPABASE_DB_URL` | No | Direct Postgres URL (for migrations) |
| `APIFY_TOKEN` | No | Only if `APIFY_ENABLED=true` |
| `GOOGLE_CLIENT_ID` | No | For Search Console integration |
| `GOOGLE_CLIENT_SECRET` | No | |
| `APP_ENV` | No | `local` / `staging` / `production` |
| `STORAGE_DIR` | No | Default: `./storage` |
| `OPENAI_COST_CAP_USD_PER_RUN` | No | Default: `5.0` |
| `CRAWL_CONCURRENCY` | No | Default: `10` |

### 4. Run the server

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

---

## Development

```powershell
# Lint
ruff check app/

# Type-check
mypy app/

# Tests
pytest

# Reset all data (dev only — destructive)
python scripts/reset_db.py
```

---

## Notes

- **CORS**: currently allows `http://localhost:3000` only. Update `main.py` for other origins.
- **Cost cap**: each run hard-caps OpenAI spend at `$5` (configurable via `OPENAI_COST_CAP_USD_PER_RUN`).
- **Crawler order**: httpx → Playwright (if JS needed) → Apify (if `APIFY_ENABLED=true`). Each stage is tried only if the previous fails.
- **Storage**: crawled HTML is saved to `./storage/{run_id}/pages/` and is gitignored. It can be swapped to Supabase Storage by pointing `STORAGE_DIR` to an adapter.
