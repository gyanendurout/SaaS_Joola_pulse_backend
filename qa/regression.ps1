#requires -Version 5
<#
.SYNOPSIS
  Backend regression test for JOOLA Pulse FastAPI.

.DESCRIPTION
  Runs (in order, stops on first failure unless -Continue):
    1. Venv health   — .venv exists, key packages importable
    2. App import    — `python -c "import app.main"` (catches syntax + import errors)
    3. Migration set — every .sql file in supabase/migrations/ has content + trailing semicolon
    4. pytest        — runs tests/api/ suite (auto-skips if uvicorn isn't running)
    5. Endpoint smoke — if uvicorn is running, hit key endpoints

  Writes c:\tmp\joola-backend-qa-passed.flag on PASS.
  Exit 0 on success, non-zero on failure.

.PARAMETER SkipEndpoints
  Skip stage 5 (no live uvicorn check).

.PARAMETER SkipPytest
  Skip stage 4 (pytest).

.PARAMETER ApiUrl
  Base URL for endpoint smoke. Default http://localhost:8000.

.PARAMETER Continue
  Don't stop on first failure.
#>

[CmdletBinding()]
param(
  [switch] $SkipEndpoints,
  [switch] $SkipPytest,
  [string] $ApiUrl = 'http://localhost:8000',
  [switch] $Continue
)

$ErrorActionPreference = 'Stop'

$backendRoot = Split-Path -Parent $PSScriptRoot
Set-Location $backendRoot

$results = @()
$startedAt = Get-Date
$venvPython = Join-Path $backendRoot '.venv\Scripts\python.exe'

function Record($name, $ok, $detail) {
  $script:results += [pscustomobject]@{ Stage = $name; Ok = $ok; Detail = $detail }
  $icon = if ($ok) { '[PASS]' } else { '[FAIL]' }
  Write-Host "$icon $name $(if ($detail) { "- $detail" })"
  if (-not $ok -and -not $Continue) {
    Write-Host ''
    Write-Host "Regression aborted at: $name" -ForegroundColor Red
    exit 1
  }
}

function Test-ApiServer {
  try {
    Invoke-WebRequest -Uri "$ApiUrl/docs" -Method Head -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop | Out-Null
    return $true
  } catch { return $false }
}

Write-Host '=== JOOLA Pulse Backend Regression ===' -ForegroundColor Cyan
Write-Host "Root: $backendRoot"
Write-Host "Started: $startedAt"
Write-Host ''

# --- Stage 1: venv health ---
Write-Host '--- Stage 1: venv health ---' -ForegroundColor Yellow
if (-not (Test-Path $venvPython)) {
  Record 'venv' $false "No .venv at $venvPython — run: python -m venv .venv ; .venv\Scripts\Activate.ps1 ; pip install -e .[dev]"
} else {
  $importCheck = & $venvPython -c "import fastapi, supabase, openai, pydantic, httpx; print('ok')" 2>&1
  $venvOk = ($LASTEXITCODE -eq 0 -and ($importCheck -join '') -match 'ok')
  $venvDetail = if ($venvOk) { 'fastapi+supabase+openai+pydantic+httpx importable' } else { ($importCheck | Select-Object -First 3) -join '; ' }
  Record 'venv' $venvOk $venvDetail
}

# --- Stage 2: app import ---
Write-Host ''
Write-Host '--- Stage 2: app.main import ---' -ForegroundColor Yellow
if (Test-Path $venvPython) {
  $appImport = & $venvPython -c "import app.main; print('routes:', len(app.main.app.routes))" 2>&1
  $appOk = ($LASTEXITCODE -eq 0 -and ($appImport -join '') -match 'routes:')
  $appDetail = if ($appOk) { ($appImport | Select-Object -First 1).ToString().Trim() } else { ($appImport | Select-Object -Last 5) -join '; ' }
  Record 'app-import' $appOk $appDetail
} else {
  Record 'app-import' $false 'skipped - venv missing'
}

# --- Stage 3: migration files ---
Write-Host ''
Write-Host '--- Stage 3: migration files ---' -ForegroundColor Yellow
$migrationDir = Join-Path $backendRoot 'supabase\migrations'
if (-not (Test-Path $migrationDir)) {
  Record 'migrations' $false "No migrations dir at $migrationDir"
} else {
  $migrations = Get-ChildItem -Path $migrationDir -Filter '*.sql' | Sort-Object Name
  $migrationFails = @()
  foreach ($m in $migrations) {
    $content = Get-Content -Raw -Path $m.FullName
    if ($content.Trim().Length -eq 0) {
      $migrationFails += "$($m.Name):empty"
      continue
    }
    $last = $content.TrimEnd()
    if (-not ($last.EndsWith(';') -or $last.EndsWith('*/') -or $last.EndsWith('--'))) {
      $migrationFails += "$($m.Name):no-trailing-semicolon"
    }
  }
  $migOk = ($migrationFails.Count -eq 0)
  $migDetail = if ($migOk) { "$($migrations.Count) migrations OK" } else { "$($migrationFails.Count) issue(s): $($migrationFails -join ', ')" }
  Record 'migrations' $migOk $migDetail
}

# --- Stage 4: pytest ---
if (-not $SkipPytest) {
  Write-Host ''
  Write-Host '--- Stage 4: pytest tests/api/ ---' -ForegroundColor Yellow
  $testsDir = Join-Path $backendRoot 'tests'
  if (-not (Test-Path $testsDir)) {
    Write-Host "No tests/ directory found - skipping" -ForegroundColor DarkGray
    Record 'pytest' $true 'skipped (no tests/ dir)'
  } elseif (-not (Test-Path $venvPython)) {
    Record 'pytest' $false 'skipped - venv missing'
  } else {
    $pytestOut = & $venvPython -m pytest tests/api/ -q --tb=short 2>&1
    $pytestOk = ($LASTEXITCODE -eq 0)
    # Last line usually shows "X passed" or "X failed"
    $summary = ($pytestOut | Where-Object { $_ -match 'passed|failed|error|skipped' } | Select-Object -Last 2) -join '; '
    $pytestDetail = if ($summary) { $summary } elseif ($pytestOk) { 'tests passed' } else { ($pytestOut | Select-Object -Last 5) -join '; ' }
    Record 'pytest' $pytestOk $pytestDetail
    if (-not $pytestOk) {
      Write-Host ''
      Write-Host '--- pytest output ---' -ForegroundColor DarkGray
      $pytestOut | Select-Object -Last 20 | ForEach-Object { Write-Host "  $_" }
    }
  }
} else {
  Write-Host '--- Stage 4: skipped (-SkipPytest) ---' -ForegroundColor DarkGray
}

# --- Stage 5: endpoint smoke ---
if (-not $SkipEndpoints) {
  Write-Host ''
  Write-Host "--- Stage 5: endpoint smoke ($ApiUrl) ---" -ForegroundColor Yellow

  if (-not (Test-ApiServer)) {
    Write-Host "API not reachable at $ApiUrl - skipping endpoint smoke (OK in CI)" -ForegroundColor DarkGray
    Record 'endpoint-smoke' $true 'skipped (no uvicorn)'
  } else {
    $endpoints = @(
      @{ path = '/docs';                        expected = 200 },
      @{ path = '/openapi.json';                expected = 200 },
      @{ path = '/api/runs';                    expected = 200 },
      @{ path = '/api/news/articles?limit=1';   expected = 200 },
      @{ path = '/api/news/analytics/summary';  expected = 200 },
      @{ path = '/api/content/drafts';          expected = 200 },
      @{ path = '/api/content/templates';       expected = 200 }
    )

    $epFails = @()
    foreach ($e in $endpoints) {
      try {
        $resp = Invoke-WebRequest -Uri "$ApiUrl$($e.path)" `
                                  -SkipHttpErrorCheck `
                                  -UseBasicParsing `
                                  -ErrorAction Stop `
                                  -TimeoutSec 15
        $code = $resp.StatusCode
      } catch {
        $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
      }
      if ($code -eq $e.expected) {
        Write-Host "  [ok] $($e.path) -> $code"
      } else {
        Write-Host "  [FAIL] $($e.path) -> $code (expected $($e.expected))" -ForegroundColor Red
        $epFails += "$($e.path):$code"
      }
    }
    $epOk = ($epFails.Count -eq 0)
    $epDetail = if ($epOk) { "$($endpoints.Count) endpoints OK" } else { "$($epFails.Count) failed: $($epFails -join ', ')" }
    Record 'endpoint-smoke' $epOk $epDetail
  }
} else {
  Write-Host '--- Stage 5: skipped (-SkipEndpoints) ---' -ForegroundColor DarkGray
}

# --- Summary ---
$finishedAt = Get-Date
$elapsed = ($finishedAt - $startedAt).TotalSeconds
$failed = ($results | Where-Object { -not $_.Ok }).Count

Write-Host ''
Write-Host '=== Summary ===' -ForegroundColor Cyan
foreach ($r in $results) {
  $icon = if ($r.Ok) { '[PASS]' } else { '[FAIL]' }
  Write-Host "$icon $($r.Stage) - $($r.Detail)"
}
Write-Host ''
Write-Host ("Elapsed: {0:N1}s  Failed: $failed/$($results.Count)" -f $elapsed)

$flagPath = 'c:\tmp\joola-backend-qa-passed.flag'
$null = New-Item -Path 'c:\tmp' -ItemType Directory -Force -ErrorAction SilentlyContinue
if ($failed -gt 0) {
  Remove-Item $flagPath -ErrorAction SilentlyContinue
  exit 1
} else {
  "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') backend PASS" | Set-Content -Path $flagPath -Encoding UTF8
  Write-Host "QA pass flag written: $flagPath" -ForegroundColor Green
  exit 0
}
