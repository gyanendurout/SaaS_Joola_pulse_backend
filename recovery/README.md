# JOOLA Pulse Backend — Disaster Recovery

> **Premise:** If this repo, the GitHub remote, Supabase, and the Railway service were all wiped today, this folder contains everything needed to rebuild the JOOLA Pulse backend + database + scrapers from scratch.

**Snapshot date:** 2026-05-19
**Repo root:** `backend/`
**Stack:** FastAPI · Python 3.11 · Supabase Postgres · OpenAI · DataForSEO · Apify · Playwright

This recovery package is **backend-owned but covers the database** (single source of truth). The frontend has its own slimmer recovery folder at `frontend/recovery/`.

---

## What's in here

| File / folder | Purpose |
|---|---|
| `RECOVERY-RUNBOOK.md` | **Start here.** Step-by-step rebuild for DB + backend + scrapers. |
| `code-architecture.md` | Map of `app/`, `agents/`, `services/`, `routes/`. |
| `database/` | Supabase schema overview + every `.sql` migration file + seed data |
| `scrapers/` | Per-platform scraper documentation: Reddit, YouTube, TikTok, X, Instagram, AI enrichment |
| `env.template` | `.env` template (no real secrets) |
| `secrets-checklist.md` | Where each secret comes from. **Used by frontend recovery too.** |
| `railway-setup.md` | Railway backend service config |
| `github-repos.md` | Push workflow + Railway auto-deploy |
| `domain-dns.md` | Custom domain (none configured) |

---

## Stack summary

| Layer | Tech | Where |
|---|---|---|
| API | FastAPI · uvicorn | `app/main.py` + `app/routes/` |
| Agents | OpenAI gpt-4o-mini + gpt-4o | `app/agents/` |
| DB client | `supabase-py` w/ service-role key | `app/db.py` |
| External APIs | DataForSEO (SEO), Apify (IG), OpenAI | `app/services/` |
| Deploy | Railway service, auto-deploys on push to `main` | GitHub `SaaS_Joola_pulse_backend` |

---

## JOOLA brand identity (shared with frontend)

`brand_id = 04db8591-37a3-4634-9d11-536975fa6935`
Hard-coded as constant in scrapers + every social-media frontend `page.tsx`.

---

## Critical backend-only gaps

1. **No migrations in source for:** `joola_ig_*` (8 tables), `yt_channels`/`yt_videos`/`yt_channel_weekly`, `tiktok_accounts`/`tiktok_videos`, `x_accounts`/`x_posts`, `reddit_mentions`, `influencers`/`influencer_posts`, `brands`. Schemas have to be inferred from frontend TypeScript types (documented in `database/schema-overview.md`).
2. **YouTube / TikTok / X / Reddit scrapers** not present in repo. Data exists in DB but ingestion code does not. Likely external (Apify console / manual / separate repo).
3. **AI enrichment pipeline** runs only on Instagram (`scrape_joola_ig.py` — lives in `frontend/scripts/` for historical reasons). TikTok, X, Reddit sentiment/topics/crisis columns are `null` because no enrichment script exists for them.
4. **Railway dashboard configuration** (env vars, build commands) cannot be read from the filesystem. Document manually via screenshots — see `railway-setup.md`.
5. **Apify actor configs** referenced (`apify/instagram-profile-scraper`, `apify/instagram-comment-scraper`) but actor input schemas are not stored locally.

---

## How to use this backup

1. Read [RECOVERY-RUNBOOK.md](./RECOVERY-RUNBOOK.md) end-to-end before doing anything.
2. Apply migrations under `database/migrations/` **in numeric order** (001 → 007) in the Supabase SQL editor.
3. Hand-create the missing tables documented in `database/schema-overview.md` (§ "Tables with no migration source").
4. Seed `brands` and `influencers` per `database/seed-data.md`.
5. Fill `.env` per [env.template](./env.template) + [secrets-checklist.md](./secrets-checklist.md).
6. Push code to a fresh GitHub repo per [github-repos.md](./github-repos.md).
7. Wire Railway per [railway-setup.md](./railway-setup.md).
8. Run scrapers per `scrapers/`.
9. Run `qa/regression.ps1` to verify everything imports + key endpoints respond.
