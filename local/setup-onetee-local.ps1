# setup-onetee-local.ps1
# One-time setup for the OneTee local runner on Windows.
#   1. ensures Python is installed
#   2. downloads (or updates) the onetee-scraper repo next to this script
#   3. installs the Python dependencies
#   4. creates local.env for your Cloudflare API token
#   5. registers a scheduled task that runs the pull every 15 minutes
#
# Re-running this script is safe: it updates the repo and re-registers the task.
# Right-click -> "Run with PowerShell", or from a PowerShell window:
#   powershell -ExecutionPolicy Bypass -File .\setup-onetee-local.ps1

$ErrorActionPreference = "Stop"
$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo    = Join-Path $here "onetee-scraper"
$envFile = Join-Path $here "local.env"
$run     = Join-Path $here "run-onetee-local.ps1"

# 1. Python -----------------------------------------------------------------
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py -or (& python --version 2>&1) -notmatch "Python 3") {
    Write-Host "Python 3 not found - installing via winget..." -ForegroundColor Yellow
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    Write-Host ""
    Write-Host "Python installed. CLOSE this window, open a NEW PowerShell," -ForegroundColor Cyan
    Write-Host "and run setup-onetee-local.ps1 again to finish." -ForegroundColor Cyan
    return
}
Write-Host ("Python OK: " + (& python --version 2>&1)) -ForegroundColor Green

# 2. repo -------------------------------------------------------------------
$gitUrl = "https://github.com/OneTeeApp/onetee-scraper.git"
$zipUrl = "https://github.com/OneTeeApp/onetee-scraper/archive/refs/heads/main.zip"
if (Test-Path (Join-Path $repo ".git")) {
    Write-Host "Updating repo (git pull)..."
    git -C $repo pull
} elseif (Get-Command git -ErrorAction SilentlyContinue) {
    Write-Host "Cloning repo..."
    git clone $gitUrl $repo
} else {
    Write-Host "git not found - downloading repo zip..."
    $zip = Join-Path $here "repo.zip"
    Invoke-WebRequest $zipUrl -OutFile $zip
    if (Test-Path $repo) { Remove-Item $repo -Recurse -Force }
    Expand-Archive $zip -DestinationPath $here -Force
    Rename-Item (Join-Path $here "onetee-scraper-main") $repo
    Remove-Item $zip
}
Write-Host "Repo ready at $repo" -ForegroundColor Green

# 3. dependencies -----------------------------------------------------------
Write-Host "Installing Python dependencies..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet --upgrade requests beautifulsoup4
Write-Host "Dependencies installed." -ForegroundColor Green

# 4. credentials ------------------------------------------------------------
if (!(Test-Path $envFile)) {
    Copy-Item (Join-Path $here "local.env.example") $envFile
    Write-Host ""
    Write-Host "Created local.env. Opening it now - paste your Cloudflare API" -ForegroundColor Cyan
    Write-Host "token where it says 'paste-your-d1-token-here', save, then run" -ForegroundColor Cyan
    Write-Host "setup-onetee-local.ps1 again to finish." -ForegroundColor Cyan
    Start-Process notepad $envFile
    return
}
if ((Get-Content $envFile -Raw) -match "paste-your-d1-token-here") {
    Write-Host ""
    Write-Host "local.env still has the placeholder token. Open it, paste your" -ForegroundColor Yellow
    Write-Host "Cloudflare API token, save, then run this script again." -ForegroundColor Yellow
    Start-Process notepad $envFile
    return
}
Write-Host "Credentials found in local.env." -ForegroundColor Green

# 5. scheduled task ---------------------------------------------------------
$taskName = "OneTee EZLinks pull"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$run`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Pull Cloudflare-gated golf tee times from this PC and push to D1" -Force | Out-Null

Write-Host ""
Write-Host "Done. Scheduled task '$taskName' runs every 15 minutes while you're logged in." -ForegroundColor Green
Write-Host "Running it once now to verify..." -ForegroundColor Cyan
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 45
Write-Host "----- last-run.log -----"
Get-Content (Join-Path $here "last-run.log") -Tail 30
