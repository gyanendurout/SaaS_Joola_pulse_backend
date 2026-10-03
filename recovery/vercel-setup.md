# Vercel setup

> Hosting moved from Railway to Vercel on 2026-10-03. The old Railway services (and the
> `saasjoolapulsefrontend-production.up.railway.app` URL) are retired.

## High-level architecture

Three separate Vercel projects, each linked to a separate GitHub repo. Auto-deploy on push to `main`.

```
GitHub: gyanendurout/SaaS_Joola_pulse_frontend           ──►  Vercel project: frontend   (Next.js 14)
GitHub: gyanendurout/SaaS_Joola_pulse_backend            ──►  Vercel project: backend    (FastAPI, Python function)
GitHub: gyanendurout/SaaS_Joola_pulse_analytics_backend  ──►  Vercel project: analytics  (FastAPI, Python function + Cron)
                                              ▲
                                              │ /seo-api/*       → ${SEO_API_URL}/api/*
                                              │ /analytics-api/* → ${ANALYTICS_API_URL}/*
                                              │ (Next.js rewrites in next.config.mjs)
                                              │
                    Frontend reads SEO_API_URL / ANALYTICS_API_URL = backend / analytics Vercel URLs
```

| Service | Local source | Staging repo | GitHub repo |
|---|---|---|---|
| Frontend (Next.js 14) | `frontend\` | `C:\tmp\joola-frontend` | `gyanendurout/SaaS_Joola_pulse_frontend` |
| Backend (FastAPI — SEO/news/paddles/content) | `backend\` | `C:\tmp\joola-backend` | `gyanendurout/SaaS_Joola_pulse_backend` |
| Analytics (FastAPI, statsmodels) | `analytics_backend\` | `C:\tmp\joola-analytics-backend` | `gyanendurout/SaaS_Joola_pulse_analytics_backend` |

> **CRITICAL:** Environment variables, domains and plan settings live only in the Vercel dashboard. They are **not readable from the filesystem**. After every restoration, screenshot Project → Settings → Environment Variables and Project → Settings → Domains for all three projects and commit those PNGs under this folder for the next recovery.

## Creating a project (same steps for all three)

1. Vercel → Add New → Project → **Import** the GitHub repo.
2. Framework preset is auto-detected: **Next.js** for the frontend; **Python / "Other"** for the two backends.
3. Root directory = **repo root** (each staging repo is already just that one service).
4. Set the environment variables listed below.
5. Deploy. Every later push to `main` auto-deploys.

## Project: backend

### Source

- GitHub repo: `gyanendurout/SaaS_Joola_pulse_backend`
- Branch: `main`
- Auto-deploy on push: ON

### Build

- `api/index.py` is the Vercel Python entrypoint; it imports `app.main:app`.
- `vercel.json` rewrites every route to `/api/index` and sets `maxDuration: 300`.
- `.vercelignore` keeps local-only files out of the upload.
- Dependencies installed from `requirements.txt`.
- **Playwright is not installed on Vercel.** It is an optional extra for local use only (`pip install -e ".[js]"`). When it is absent the crawler skips JS rendering.
- Scratch files are written to `/tmp`, which is ephemeral per function instance.

### Required environment variables

Copy every key from `backend/recovery/env.template`, set real values. Critical ones:

| Variable | Note |
|---|---|
| `OPENAI_API_KEY` | Required |
| `OPENAI_MODEL_CHEAP` | `gpt-4o-mini` |
| `OPENAI_MODEL_SMART` | `gpt-4o` |
| `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD` | Required for SEO agents |
| `SUPABASE_URL` | Same value as frontend's `NEXT_PUBLIC_SUPABASE_URL` |
| `SUPABASE_SERVICE_ROLE_KEY` | **Never goes in the frontend project.** |
| `APP_ENV` | Set to `production` here |
| `APIFY_TOKEN`, `APIFY_ENABLED` | Optional; set if you want Apify-based crawling |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Optional — GSC OAuth |
| `GOOGLE_REDIRECT_BASE_URL` | The backend's Vercel URL |
| `CORS_ORIGINS` | Comma-separated; must include the frontend Vercel URL |
| `STORAGE_DIR` | **Do NOT set on Vercel** — the app uses `/tmp` there |

### Domain

- Project → Settings → Domains shows the generated `<backend>.vercel.app` URL.
- Copy that URL (no trailing slash) into the frontend project's `SEO_API_URL`.

## Project: analytics

### Source

- GitHub repo: `gyanendurout/SaaS_Joola_pulse_analytics_backend`
- Branch: `main`
- Auto-deploy on push: ON

### Build

- `api/index.py` is the entrypoint; `vercel.json` rewrites to `/api/index`, `maxDuration: 300`.
- Dependencies from `requirements.txt`; `.vercelignore` trims the upload.
- **Vercel Cron** (`vercel.json`): `0 6 * * *` (daily) → `GET /api/cron/pipeline`. The in-process APScheduler never runs on Vercel; Cron replaces it.

### Required environment variables

| Variable | Note |
|---|---|
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | Required |
| `OPENAI_API_KEY` | Required |
| `CRON_SECRET` | **Required.** Vercel sends `Authorization: Bearer <CRON_SECRET>` on cron calls |
| `CORS_ORIGINS` | Comma-separated; include the frontend Vercel URL |

### Domain

- Copy the generated `<analytics>.vercel.app` URL into the frontend project's `ANALYTICS_API_URL`.

## Project: frontend

### Source

- GitHub repo: `gyanendurout/SaaS_Joola_pulse_frontend`
- Branch: `main`
- Auto-deploy on push: ON

### Build

- Auto-detected as Next.js (`vercel.json` has `"framework": "nextjs"`).
- Build command: `npm run build` (Vercel default for Next.js).

### Required environment variables

| Variable | Value source |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | Supabase project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Supabase anon key — **public, RLS-enforced** |
| `SEO_API_URL` | Backend Vercel URL, `https://`, **no trailing slash** |
| `ANALYTICS_API_URL` | Analytics Vercel URL |

**DO NOT set** `SUPABASE_SERVICE_ROLE_KEY`, `OPENAI_API_KEY`, `APIFY_API_TOKEN`, or anything else listed in the frontend env template that's only used by the Python scraper. The Next.js runtime doesn't need them, and shipping them is a leak risk.

### Domain

- Project → Settings → Domains shows `<frontend>.vercel.app`. That's the public app URL.

## Cross-project config

After all three projects are deployed and have URLs:

1. In **backend** and **analytics**, set `CORS_ORIGINS` to include the frontend's Vercel URL; set backend `GOOGLE_REDIRECT_BASE_URL` to the backend's own URL.
2. In **frontend**, set `SEO_API_URL` and `ANALYTICS_API_URL`. **Redeploy** after env-var changes — Vercel applies env vars only to new deployments.

## Known Vercel limitations

- **Function duration is capped**: 300s on Hobby, up to 800s on Pro (raise `maxDuration` in `vercel.json` on Pro). Long jobs will **not** finish inside a request:
  - paddle sync (`scripts/paddle_sync_all.py`, ~34 min)
  - news scrape
  - large SEO crawls

  Run these locally or from a scheduled machine via the existing scripts.
- **FastAPI `BackgroundTasks` are not reliable** after the response is sent on Vercel — the function may be frozen.
- **Hobby crons run at most once per day** (hence the daily analytics cron). Hourly needs Pro.
- **Function size limit:** the analytics deps (numpy/scipy/pandas/statsmodels) are large. If an analytics deploy fails on the function size limit, that is the cause.
- `/tmp` is ephemeral; nothing written there survives between invocations.

## Logs and monitoring

- Project → Deployments → click a deployment → Logs (build) / Project → Logs (runtime).
- No external observability (Sentry, Datadog) is wired in this repo. Consider adding for production.

## Things you must capture manually after restore

The Vercel dashboard is the source of truth for the items below. Re-document them after every restore by screenshotting and saving under `backup/05-deployment/screenshots/`.

- Project → Settings → Environment Variables for all three projects
- Project → Settings → Git → connected repo + production branch
- Project → Settings → Domains
- Project → Settings → General → Framework preset / build overrides (if customized)
- Project → Settings → Cron Jobs (analytics)
