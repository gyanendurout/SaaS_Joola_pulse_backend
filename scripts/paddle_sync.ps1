<#
.SYNOPSIS
  Unattended wrapper around paddle_sync_all.py. Logs, rotates, and reports.

.DESCRIPTION
  This is what a scheduled task should call — not the Python script directly —
  because it resolves its own paths, activates nothing (uses the venv python
  explicitly), writes a timestamped log, and maps the exit code to something
  Task Scheduler surfaces usefully.

.EXAMPLE
  # one-off
  powershell -ExecutionPolicy Bypass -File .\scripts\paddle_sync.ps1

  # weekly, Mondays 03:00 — run this ONCE to install the schedule
  $ps1 = "C:\Workspace\SaaS_Joola_pulse\backend\scripts\paddle_sync.ps1"
  $act = New-ScheduledTaskAction -Execute "powershell.exe" `
           -Argument "-ExecutionPolicy Bypass -NonInteractive -File `"$ps1`""
  $trg = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 3am
  $set = New-ScheduledTaskSettingsSet -StartWhenAvailable `
           -ExecutionTimeLimit (New-TimeSpan -Hours 6) -RestartCount 2 `
           -RestartInterval (New-TimeSpan -Minutes 30)
  Register-ScheduledTask -TaskName "JoolaPulse-PaddleSync" -Action $act `
           -Trigger $trg -Settings $set -Description "Crawl paddle reviews -> Supabase"

  # monthly instead: -Weekly ... becomes
  #   New-ScheduledTaskTrigger -Monthly -DaysOfMonth 1 -At 3am
#>
[CmdletBinding()]
param(
    [string] $Sources,          # optional comma list, e.g. "yotpo,judgeme"
    [switch] $SkipEnrich,       # crawl + load, spend no LLM budget
    [int]    $KeepLogs = 20     # how many run logs to retain
)

$ErrorActionPreference = 'Stop'

$BackendDir = Split-Path -Parent $PSScriptRoot
$Python     = Join-Path $BackendDir '.venv\Scripts\python.exe'
$Script     = Join-Path $PSScriptRoot 'paddle_sync_all.py'
$LogDir     = Join-Path $BackendDir 'logs'

if (-not (Test-Path $Python)) { throw "venv python not found at $Python" }
if (-not (Test-Path $Script)) { throw "runner not found at $Script" }
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

$stamp   = Get-Date -Format 'yyyyMMdd-HHmmss'
$logFile = Join-Path $LogDir "paddle_sync_$stamp.log"

$pyArgs = @($Script)
if ($Sources)   { $pyArgs += @('--sources', $Sources) }
if ($SkipEnrich){ $pyArgs += '--skip-enrich' }

"=== paddle sync started $(Get-Date -Format s) ===" | Out-File $logFile -Encoding utf8
"args: $($pyArgs -join ' ')"                        | Out-File $logFile -Encoding utf8 -Append

# Push-Location so relative paths inside the script (storage/, logs/) resolve.
Push-Location $BackendDir
try {
    & $Python @pyArgs *>&1 | Tee-Object -FilePath $logFile -Append
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
}

"=== finished $(Get-Date -Format s) exit=$code ===" | Out-File $logFile -Encoding utf8 -Append

# Keep the log directory from growing without bound.
Get-ChildItem $LogDir -Filter 'paddle_sync_*.log' |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip $KeepLogs |
    Remove-Item -Force -ErrorAction SilentlyContinue

switch ($code) {
    0 { Write-Host "paddle sync OK -> $logFile" }
    1 { Write-Warning "paddle sync PARTIAL (a source failed) -> $logFile" }
    default { Write-Error "paddle sync FAILED (exit $code) -> $logFile" }
}

exit $code
