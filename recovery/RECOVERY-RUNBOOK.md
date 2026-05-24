# Backend Recovery Runbook

> **Audience:** A senior dev with no prior context, who has the contents of `backend/` and an empty machine.
> **Goal:** Re-deploy a working JOOLA Pulse backend + Supabase + scrapers within a working day.
> **Snapshot:** 2026-05-19

Run this **before** `frontend/recovery/RECOVERY-RUNBOOK.md` — the frontend depends on Supabase being up and the backend Railway URL being known.

---

## Phase 0 — Prerequisites

Accounts / API keys needed:

- **Supabase** (postgres host + auth) — free tier OK
- **OpenAI** (`gpt-4o-mini` + `gpt-4o`) — billing enabled
- **DataForSEO** (SEO keyword + SERP data) — paid; account `api@joola.com`
- **Apify** (Instagram scraping) — paid; existing token has prebuilt actors associated
- **Railway** (backend hosting)
- **GitHub** (`SaaS_Joola_pulse_backend` repo)
- *(Optional)* **Google Cloud** (GSC + GA4 OAuth — feature deferred, can skip)
- *(Optional)* **Photoroom** (image bg removal — low priority)

Tooling: **Python 3.11+**, `git`, **PowerShell** (Windows path bias — Bash hangs on Windows git credential prompts).

---

## Phase 1 — Supabase project

1. https://supabase.com → New project. Pick `us-east-1` (matches previous URL `loecyghnkkxyymelgexz.supabase.co`).
2. Record from Project Settings → API:
   - `URL` → `NEXT_PUBLIC_SUPABASE_URL` (frontend) + `SUPABASE_URL` (backend)
   - `anon` (public) key → `NEXT_PUBLIC_SUPABASE_ANON_KEY`
   - `service_role` (secret) key → `SUPABASE_SERVICE_ROLE_KEY` (backend + scrapers only; **never** browser)
3. Open SQL editor and apply migrations from `database/migrations/` in this **exact order**:

   ```
   001_init.sql
   002_single_url.sql
   003_extended.sql
   004_seed_keywords.sql
   005_content.sql
   006_news_tables.sql
   007_seo_columns.sql
   ```
   (`_apply_all_pending.sql` applies 002+003+004 in one shot; safe alternative.)

4. **CRITICAL — Hand-create the tables not covered by migrations.**
   The repo does not contain migrations for `brands`, `joola_ig_*`, `yt_*`, `tiktok_*`, `x_*`, `reddit_mentions`, `influencers`, `influencer_posts`. Use `database/schema-overview.md` (sections marked "Tables with no migration source") to write `CREATE TABLE` statements. Must be done before any scraper runs successfully.

5. Database → Replication → ensure default publication is on.

---

## Phase 2 — Seed core tables

Full list + source citations in `database/seed-data.md`. Minimum:

```sql
-- 11 brands (JOOLA is is_joola = true)
insert into brands (id, name, is_joola) values
  ('04db8591-37a3-4634-9d11-536975fa6935', 'JOOLA', true)
  -- + 10 others: Selkirk, Paddletek, Franklin, CRBN, Engage, Onix, Six Zero, Proton, Head, Wilson
;

-- 6 JOOLA athletes (full list of 35 in news_scraper.py)
insert into influencers (brand_id, name, instagram_handle, youtube_channel_url) values
  ('04db8591-37a3-4634-9d11-536975fa6935', 'Ben Johns',          'benjohnspb', 'https://www.youtube.com/@BenJohns'),
  ('04db8591-37a3-4634-9d11-536975fa6935', 'Collin Johns',       'collinjohns', null),
  ('04db8591-37a3-4634-9d11-536975fa6935', 'Anna Bright',        'anna.bright', null),
  ('04db8591-37a3-4634-9d11-536975fa6935', 'Tyson McGuffin',     'tysonmcguffin', null),
  ('04db8591-37a3-4634-9d11-536975fa6935', 'Federico Staksrud',  'fedestaksrud', null),
  ('04db8591-37a3-4634-9d11-536975fa6935', 'Simone Jardim',      'simonejardim10', null);
-- ^ Verify handles by visiting each IG/YT profile.
```

`news_sources` (20 pickleball sites) seeds itself via migration 006.

---

## Phase 3 — Restore source

```powershell
cd c:\Workspace\SaaS_Joola_pulse\backend
git init
git remote add origin https://github.com/<your-org>/SaaS_Joola_pulse_backend.git
```

`.env` must already be in `.gitignore` before staging anything.

---

## Phase 4 — Environment

```powershell
copy recovery\env.template .env
```

Required values per [secrets-checklist.md](./secrets-checklist.md):

| Var | Source |
|---|---|
| `SUPABASE_URL` | Supabase Settings → API → URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase Settings → API → service_role |
| `OPENAI_API_KEY` | OpenAI dashboard |
| `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD` | DataForSEO dashboard |
| `APIFY_TOKEN` | Apify console |
| `BRAND_ID` | `04db8591-37a3-4634-9d11-536975fa6935` (JOOLA) |

---

## Phase 5 — Local smoke test

```powershell
cd c:\Workspace\SaaS_Joola_pulse\backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
# → open http://localhost:8000/docs and verify the API tree loads
```

Then run the regression script:

```powershell
.\qa\regression.ps1
```

Must exit 0. See [qa/README.md](../qa/README.md).

---

## Phase 6 — Push to GitHub

```powershell
git add .
git commit -m "Restore backend from disaster-recovery backup"
git push -u origin main
```

---

## Phase 7 — Wire Railway

See [railway-setup.md](./railway-setup.md). Summary:

1. Railway → New Service → Deploy from GitHub → pick `SaaS_Joola_pulse_backend`.
2. Auto-detects `Procfile`: `web: uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}`.
3. Variables: paste every key from `.env` (one per row — do NOT paste the whole file).
4. Networking → Generate domain → record the URL. The frontend will need it as `SEO_API_URL`.
5. First deploy triggers on push to `main`.

---

## Phase 8 — Run scrapers (initial backfill)

### Instagram (the only fully self-contained scraper)

Lives at `frontend/scripts/scrape_joola_ig.py`:

```powershell
cd c:\Workspace\SaaS_Joola_pulse\frontend
python scripts/scrape_joola_ig.py
```

Reads `frontend/.env.local`, hits Apify (`apify/instagram-profile-scraper` + `apify/instagram-comment-scraper`), runs OpenAI analysis, writes to all `joola_ig_*` tables. ~15–30 min. Idempotent. See `scrapers/instagram-scraper.md`.

### News (20 pickleball sites)

```powershell
cd c:\Workspace\SaaS_Joola_pulse\backend
.venv\Scripts\Activate.ps1
python scrape_now.py
# OR once backend is up:
# curl -X POST http://localhost:8000/api/news/scrape
```

### YouTube / TikTok / X / Reddit

**These scrapers are not in the repo.** Existing rows came from an external pipeline. Options:

- Check Apify console for actors/tasks under the user ID in `secrets-checklist.md`.
- If gone, rebuild per `scrapers/*.md` — each file lists evidence + a rebuild sketch.
- Manual fallback: tables can stay empty; dashboard pages render "no data" gracefully.

### AI enrichment

IG enriches inline. **No enrichment script exists for TikTok / X / Reddit** — that's why those `sentiment_label`, `topics`, `is_crisis` columns are null. To enrich: see `scrapers/ai-enrichment-pipeline.md` for a rebuild outline.

---

## Phase 9 — Verification

```bash
# Backend API
curl https://<railway-backend-url>/docs   # OpenAPI page loads
curl https://<railway-backend-url>/api/runs   # JSON list (may be empty)

# Trigger an SEO crawl against joola.com
curl -X POST https://<railway-backend-url>/api/analyze \
  -H 'content-type: application/json' \
  -d '{"url":"https://joola.com"}'
```

Run the regression script one more time after deploy. See [qa/README.md](../qa/README.md).

---

## Known backend gaps the runbook cannot resolve automatically

| # | Gap | Mitigation |
|---|---|---|
| 1 | No SQL migrations for `brands`, `joola_ig_*`, `yt_*`, `tiktok_*`, `x_*`, `reddit_mentions`, `influencers`, `influencer_posts` | Reconstruct from `database/schema-overview.md`. Save the resulting `CREATE TABLE` statements as `008_social_and_ig_tables.sql` in the recovered repo so future recoveries are clean. |
| 2 | YouTube / TikTok / X / Reddit scrapers not in repo | Check Apify console first; otherwise rebuild per `scrapers/*.md` |
| 3 | AI enrichment only for Instagram | Port patterns from `frontend/scripts/scrape_joola_ig.py` to per-platform files. See `scrapers/ai-enrichment-pipeline.md`. |
| 4 | Railway env vars + build settings live only in Railway dashboard | After restoration, screenshot Service → Variables and Service → Settings and commit them here for next time. |
| 5 | Google OAuth credentials (GSC/GA4) | Feature deferred — leave blank. Recreate via Google Cloud Console (instructions in `secrets-checklist.md`). |
| 6 | Apify actor input schemas | Re-input via Apify console → Actor → Input tab. Document at restore time. |
