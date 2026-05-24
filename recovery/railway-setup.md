# Railway setup

## High-level architecture

Two separate Railway services, each linked to a separate GitHub repo. Auto-deploy on push to `main`.

```
GitHub: SaaS_Joola_pulse_frontend  ──►  Railway service: joola-pulse-frontend  (Next.js)
GitHub: SaaS_Joola_pulse_backend   ──►  Railway service: joola-pulse-backend   (FastAPI)
                                              ▲
                                              │ /seo-api/* proxy via Next.js rewrite
                                              │
                                  Frontend reads SEO_API_URL = backend Railway URL
```

> **CRITICAL:** All Railway configuration (service settings, env vars, regions) lives only in the Railway dashboard. It is **not readable from the filesystem**. After every restoration, screenshot Service → Variables and Service → Settings and commit those PNGs under this folder for the next recovery.

## Service: joola-pulse-backend

### Source

- GitHub repo: `SaaS_Joola_pulse_backend`
- Branch: `main`
- Auto-deploy on push: ON

### Build

- Detected as Python automatically.
- `Procfile` provides start command: `web: uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}`
- Dependencies installed from `requirements.txt`.
- **Playwright caveat:** the backend depends on `playwright>=1.48`. On Railway's Nixpacks build, you may need to add a `nixpacks.toml` or a custom Dockerfile that runs `playwright install chromium` after `pip install`. Confirm Playwright-based crawls work after first deploy.

### Required environment variables

Copy every key from `02-environment/backend.env.template`, set real values. Critical ones:

| Variable | Note |
|---|---|
| `OPENAI_API_KEY` | Required |
| `OPENAI_MODEL_CHEAP` | `gpt-4o-mini` |
| `OPENAI_MODEL_SMART` | `gpt-4o` |
| `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD` | Required for SEO agents |
| `SUPABASE_URL` | Same value as frontend's `NEXT_PUBLIC_SUPABASE_URL` |
| `SUPABASE_SERVICE_ROLE_KEY` | **Never goes in the frontend service.** |
| `APP_ENV` | Set to `production` here |
| `APIFY_TOKEN`, `APIFY_ENABLED` | Optional; set if you want Apify-based crawling |
| `GOOGLE_*` | Optional — GSC OAuth |
| `STORAGE_DIR` | `/tmp/storage` works on Railway (ephemeral, fine for POC) |

### Networking

- Service → Settings → Networking → Generate Domain. You get something like `joola-pulse-backend.up.railway.app`.
- Copy that URL into the frontend service's `SEO_API_URL` env var.

### Resource sizing

Default Railway plan (512MB RAM, 1 vCPU) is enough for the news scraper and SEO crawls of small-to-medium sites (<300 pages). For larger crawls, scale up.

## Service: joola-pulse-frontend

### Source

- GitHub repo: `SaaS_Joola_pulse_frontend`
- Branch: `main`
- Auto-deploy on push: ON

### Build

- Auto-detected as Next.js (`vercel.json` exists with `"framework": "nextjs"` but Railway uses its own detector).
- Build command: `npm run build` (or `next build`).
- Start command: `npm run start` (uses `next start`).
- Node version: 18+ (Railway default in May 2026 is Node 20).

### Required environment variables

| Variable | Value source |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | Supabase project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Supabase anon key — **public, RLS-enforced** |
| `SEO_API_URL` | The deployed backend service URL from above (with `https://`) |

**DO NOT set** `SUPABASE_SERVICE_ROLE_KEY`, `OPENAI_API_KEY`, `APIFY_API_TOKEN`, or anything else listed in the frontend env template that's only used by the Python scraper. The Next.js runtime doesn't need them, and shipping them is a leak risk.

### Networking

- Service → Settings → Networking → Generate Domain. You get `joola-pulse-frontend.up.railway.app` or similar.
- That's the public app URL.

## Cross-service config

After both services are deployed and have URLs:

1. In **backend** Railway dashboard, no frontend URL needed. Backend is CORS-permissive in dev — if you tighten CORS for prod, allow the frontend's Railway domain.
2. In **frontend** Railway dashboard, set `SEO_API_URL` = backend URL. **Trigger a redeploy** of the frontend after this env var change (Railway doesn't always auto-redeploy on env-only changes).

## Logs and monitoring

- Service → Deployments → click a deployment → View logs.
- Railway retains logs for ~7 days on free tier, longer on paid.
- No external observability (Sentry, Datadog) is wired in this repo. Consider adding for production.

## Cost note

Two Railway services on the Hobby plan (~$5/mo each, plus metered usage) is approximately $10–25/month total at the data volumes this project handles.

## Things you must capture manually after restore

The Railway dashboard is the source of truth for the items below. Re-document them after every restore by screenshotting and saving under `backup/05-deployment/screenshots/`.

- Service → Variables tab for both services
- Service → Settings → Source → Repo + branch + auto-deploy toggle
- Service → Settings → Networking → Domain
- Service → Settings → Build → Build command + start command (if customized)
- Any Cron / Schedules (Railway supports cron jobs separately — useful for periodic scrape triggers if you wire one up)
