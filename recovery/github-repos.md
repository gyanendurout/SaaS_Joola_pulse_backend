# GitHub repos + copy-from-source workflow

> **The defining oddity of this project's deploy workflow:** development happens in one set of folders (`c:\Workspace\SaaS_Joola_pulse\frontend\` and `backend\`), but deploys come from a **separate** pair of staging repos. Changes are copied file-by-file from dev folders into staging folders before each push. Project memory says this verbatim: "**NEVER push to `joola-nextjs.git` (the monorepo remote). It is not used.**"

## The two repos

| Repo | Purpose | Dev path | Staging path | Railway service |
|---|---|---|---|---|
| `SaaS_Joola_pulse_frontend.git` | Next.js app | `c:\Workspace\SaaS_Joola_pulse\frontend\` | `C:\tmp\joola-frontend\` | `joola-pulse-frontend` |
| `SaaS_Joola_pulse_backend.git`  | FastAPI app  | `c:\Workspace\SaaS_Joola_pulse\backend\`  | `C:\tmp\joola-backend\`  | `joola-pulse-backend`  |

## Deploy workflow (every code change)

1. Edit files in `c:\Workspace\SaaS_Joola_pulse\frontend\` or `backend\` as normal.
2. Copy each changed file to the matching staging repo:

   Frontend:
   ```powershell
   Copy-Item "C:\Workspace\SaaS_Joola_pulse\frontend\<relative-path>" `
             "C:\tmp\joola-frontend\<relative-path>" -Force
   ```

   Backend:
   ```powershell
   Copy-Item "C:\Workspace\SaaS_Joola_pulse\backend\<relative-path>" `
             "C:\tmp\joola-backend\<relative-path>" -Force
   ```

3. From the staging folder:
   ```powershell
   cd C:\tmp\joola-frontend     # or C:\tmp\joola-backend
   git add .
   git commit -m "<message>"
   git push origin main
   ```

4. Railway auto-deploys on push to `main`.

### Why the copy-from-source dance?

The dev folders contain stuff the staging repos should NOT have:
- `node_modules/` (huge)
- `.venv/` (huge — Python virtualenv)
- `frontend/design2/` reference jsx (not used at runtime)
- Local-only `.env` files
- Local scrape state / log files

The split also means the dev tree can experimentally break without poisoning the deploy branch.

### Git push must use PowerShell

Project memory: "Git push via PowerShell only (Bash hangs on credential prompt)". Cause: Windows git credential manager prompts via a GUI; from a Bash subshell on Windows that GUI prompt never surfaces and `git push` blocks indefinitely.

## What's in `.gitignore` for staging repos

Recommended for both staging repos:

```
# Frontend
.next/
node_modules/
.env.local
.env*.local
*.log

# Backend
__pycache__/
.venv/
.env
*.log
storage/
```

## Recovery: recreating the staging repos

After a disaster, you start with just the dev repo (this `c:\Workspace\SaaS_Joola_pulse\` tree). Recreate staging:

```powershell
# Frontend
mkdir C:\tmp\joola-frontend
Copy-Item "C:\Workspace\SaaS_Joola_pulse\frontend\*" "C:\tmp\joola-frontend\" -Recurse -Exclude @("node_modules","design2",".next",".env.local")
cd C:\tmp\joola-frontend
git init
git remote add origin https://github.com/<your-org>/SaaS_Joola_pulse_frontend.git
git add .
git commit -m "Initial restore"
git push -u origin main

# Backend
mkdir C:\tmp\joola-backend
Copy-Item "C:\Workspace\SaaS_Joola_pulse\backend\*" "C:\tmp\joola-backend\" -Recurse -Exclude @(".venv","__pycache__",".env","storage")
cd C:\tmp\joola-backend
git init
git remote add origin https://github.com/<your-org>/SaaS_Joola_pulse_backend.git
git add .
git commit -m "Initial restore"
git push -u origin main
```

## Long-term suggestion (post-recovery)

Consider eliminating the split by:

1. Adding a proper `.gitignore` to the dev repo.
2. Pushing the dev repo directly to GitHub.
3. Pointing Railway at subdirectories (`frontend/` and `backend/`) using Railway's "Root Directory" setting per service.

This collapses two parallel staging repos into one monorepo, and saves the copy-step. But it requires careful `.gitignore` hygiene (current dev tree has 1000+ files in `node_modules/` and `.venv/` that would otherwise leak in).

## Old repo to avoid

`joola-nextjs.git` was the original monorepo remote. It is **abandoned**. Never push to it. If you find references to it in the dev tree's `.git/config`, remove them.
