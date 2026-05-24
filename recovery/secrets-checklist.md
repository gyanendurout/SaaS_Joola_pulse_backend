# Secrets checklist

Every `{{PLACEHOLDER}}` in the env templates corresponds to one row here. Tick each box before running anything in production.

---

## Supabase

- [ ] **`SUPABASE_URL`** / **`NEXT_PUBLIC_SUPABASE_URL`**
  - Source: Supabase Dashboard → Your Project → Settings → API → `Project URL`
  - Format: `https://<project-ref>.supabase.co`
  - Used by: backend (`SUPABASE_URL`), frontend (`NEXT_PUBLIC_SUPABASE_URL`), IG scraper

- [ ] **`NEXT_PUBLIC_SUPABASE_ANON_KEY`** (publishable / anon key)
  - Source: Supabase Dashboard → Settings → API → Project API keys → `anon` `public`
  - **Safe to expose to browser.** Has RLS enforced.
  - Used by: frontend only

- [ ] **`SUPABASE_SERVICE_ROLE_KEY`** (secret key, bypasses RLS)
  - Source: Supabase Dashboard → Settings → API → Project API keys → `service_role` `secret`
  - **NEVER expose to browser. Never use in any `NEXT_PUBLIC_*` variable.**
  - Used by: backend, both scraper scripts

- [ ] **`SUPABASE_DB_URL`** (optional — direct postgres)
  - Source: Supabase Dashboard → Settings → Database → Connection string → URI
  - Used by: only if we bypass `supabase-py`. Leave blank unless explicitly switching.

---

## OpenAI

- [ ] **`OPENAI_API_KEY`**
  - Source: https://platform.openai.com → API keys → Create new secret key
  - Project memory note: existing key is a `sk-proj-...` (project-scoped) key. Use a project key, not a user key, so it can be revoked without affecting other tools.
  - Used by: backend (all AI calls), IG scraper (comment classification)
  - Models used: `gpt-4o-mini` (cheap classification) and `gpt-4o` (smart reasoning) — both must be enabled on the OpenAI project.

- [ ] **`OPENAI_MODEL_CHEAP`** = `gpt-4o-mini` (default, fine)
- [ ] **`OPENAI_MODEL_SMART`** = `gpt-4o` (default, fine)
- [ ] **`OPENAI_COST_CAP_USD_PER_RUN`** = `5.0` (raise once trusted)

---

## DataForSEO

- [ ] **`DATAFORSEO_LOGIN`** + **`DATAFORSEO_PASSWORD`**
  - Source: https://app.dataforseo.com → API Dashboard → API Access
  - Existing account login: `api@joola.com` (confirm with team)
  - Used by: backend SEO agents (Agent D — keyword research, Agent K — SERP / backlinks)
  - Pay-as-you-go. Check current balance before bulk runs.

---

## Apify

- [ ] **`APIFY_API_TOKEN`** / **`APIFY_TOKEN`**
  - Source: https://console.apify.com → Settings → Integrations → API tokens
  - Used by:
    - `frontend/scripts/scrape_joola_ig.py` (calls `apify/instagram-profile-scraper` + `apify/instagram-comment-scraper` actors)
    - backend (only if `APIFY_ENABLED=true` — off by default; SEO crawler can use Apify as a fetcher)
  - Apify user ID (for reference, not a secret): `7nuiUPNRN29ouzcO6`

- [ ] **`APIFY_ENABLED`** = `false` for the backend unless you want Apify-based site crawling for SEO. Instagram scraper reads `APIFY_API_TOKEN` directly and does not check this flag.

---

## Google OAuth (OPTIONAL — Phase 2 feature)

Only needed if you wire up GSC / GA4 integrations. Project memory marks these as deferred. Leave blank until you implement the feature.

- [ ] **`GOOGLE_CLIENT_ID`** + **`GOOGLE_CLIENT_SECRET`**
  - Source: https://console.cloud.google.com → APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID (Web application)
  - Add redirect URI: `<GOOGLE_REDIRECT_BASE_URL>/api/performance/callback/gsc`
  - Enable "Google Search Console API" + "Google Analytics Data API" in the same GCP project
- [ ] **`GOOGLE_REDIRECT_BASE_URL`** = your backend base URL (e.g., `https://api.joola.com` or `http://localhost:8000` for local)

---

## Photoroom (OPTIONAL — surfaced in old IG env, low priority)

Found in `frontend/.env.local`: `PHOTOROOM_API_KEY=sandbox_sk_pr_default_...`. No code reference grep'd in the recent codebase pass; likely a leftover or used by an unbuilt feature. Safe to omit unless the team confirms it's still needed.

---

## SEO_API_URL (proxy target)

- [ ] **`SEO_API_URL`** in `frontend/.env.local`
  - Local: `http://localhost:8000`
  - Production: the Railway-generated domain for the backend service, e.g. `https://saas-joola-pulse-backend.up.railway.app`
  - Wired by `next.config.mjs` → `/seo-api/:path*` rewrites to `${SEO_API_URL}/api/:path*`

---

## What to do AFTER all values are filled

1. Sanity-check the frontend: `cd frontend && npm run dev`. Open `/overview` and confirm Supabase reads work (no "RLS denied" or empty arrays caused by missing key).
2. Sanity-check the backend: `cd backend && uvicorn app.main:app --reload`. `GET http://localhost:8000/api/news/sources` should return 20 rows.
3. Rotate the JOOLA OpenAI key once the system is stable in production — the `sk-proj-i3J19uUXqCgWlVtMLyYy...` key in old envs is now considered compromised by virtue of being shipped to this backup folder context.
