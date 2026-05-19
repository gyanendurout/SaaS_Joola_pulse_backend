# Backend QA — Regression Script

`regression.ps1` is the canonical pre-push check for the JOOLA Pulse backend.

## What it covers

| Stage | Check | Detects |
|---|---|---|
| 1. Venv | `.venv\Scripts\python.exe -c "import fastapi, supabase, openai, pydantic"` | Missing deps, broken venv, dependency drift |
| 2. App import | `python -c "import app.main"` | Syntax errors, broken imports, route-registration failures |
| 3. Migrations | Every `.sql` in `supabase/migrations/` parses (semicolon check) | Truncated files, missing trailing semicolon, empty migrations |
| 4. Endpoint smoke | If uvicorn is running, HTTP GET `/docs`, `/openapi.json`, `/api/runs`, `/api/news/articles`, `/api/news/analytics/summary` | Routes that import but blow up at request time, DB-connection issues |

Stage 4 only runs if uvicorn is already reachable. If not, it's silently skipped (not a failure) — stages 1-3 alone are enough for CI / pre-push.

## What it does NOT cover

- **Live data correctness** — empty Supabase tables still return 200. To verify scrapers populated rows, query Supabase directly.
- **Pytest unit tests** — none exist yet. Add `pytest` to `[dev]` extras + a `tests/` folder when the team grows.
- **Type checking with mypy** — not wired in. `python -m mypy app/` is informational only.
- **Scraper end-to-end runs** — scrapers hit paid external APIs; not safe to include in a regression. Run manually.

## Usage

```powershell
# Full run
.\qa\regression.ps1

# CI / pre-push (no live uvicorn)
.\qa\regression.ps1 -SkipEndpoints

# Custom API URL
.\qa\regression.ps1 -ApiUrl http://localhost:8001

# Run all stages even after failure
.\qa\regression.ps1 -Continue
```

Exit code 0 = pass, non-zero = fail. Designed to be invoked by the `/end-session` orchestrator.

## When to update this script

- A new route was added that should be smoke-tested → add it to the `$endpoints` array.
- A new top-level package import is required at startup → add it to the Stage 1 import check.
- A new migration was added → it's picked up automatically (the script globs `*.sql`).
