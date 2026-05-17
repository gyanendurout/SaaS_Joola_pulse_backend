# Run the news scraper in the background.
# Output is written to logs\scrape_<timestamp>.log
# The scraper keeps running even if you close this terminal or go offline.
#
# Usage: just double-click or run from PowerShell:
#   .\run_scrape_bg.ps1

$root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$python  = "$root\.venv\Scripts\python.exe"
$script  = "$root\scrape_now.py"
$logDir  = "$root\logs"

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$stamp   = Get-Date -Format "yyyyMMdd_HHmmss"
$logFile = "$logDir\scrape_$stamp.log"

if (-not (Test-Path $python)) {
    Write-Host "ERROR: venv not found at $python" -ForegroundColor Red
    Write-Host "Run from the backend directory after activating venv." -ForegroundColor Yellow
    exit 1
}

$proc = Start-Process `
    -FilePath $python `
    -ArgumentList $script `
    -WorkingDirectory $root `
    -RedirectStandardOutput $logFile `
    -RedirectStandardError  "$logFile.err" `
    -WindowStyle Hidden `
    -PassThru

Write-Host ""
Write-Host "Scrape started (PID $($proc.Id))" -ForegroundColor Green
Write-Host "Log: $logFile" -ForegroundColor Cyan
Write-Host ""
Write-Host "To watch progress:"
Write-Host "  Get-Content '$logFile' -Wait"
Write-Host ""
Write-Host "To stop the scrape:"
Write-Host "  Stop-Process -Id $($proc.Id)"
