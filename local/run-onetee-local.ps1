# run-onetee-local.ps1
# Pulls the Cloudflare-gated golf platforms (EZLinks, and later GolfNow) from
# THIS PC's residential IP and pushes them to Cloudflare D1.
#
# Safe to run alongside the GitHub Actions job: the D1 sync only ever
# deactivates rows for courses it actually scraped this run, so this job
# touches ONLY the platforms listed in $Platforms below and never the ~72
# courses GitHub manages.
#
# Runs today + the next 2 days (UTC, to match the GitHub job's dating).

$ErrorActionPreference = "Stop"
$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo    = Join-Path $here "onetee-scraper"
$envFile = Join-Path $here "local.env"
$log     = Join-Path $here "last-run.log"

# --- platforms this residential runner is responsible for ---
$Platforms = "ezlinks"        # add ",golfnow" here once that adapter is built

"==== OneTee local pull @ $(Get-Date -Format o) ====" | Out-File $log

# --- load credentials from local.env (KEY=VALUE lines) ---
if (!(Test-Path $envFile)) {
    throw "Missing $envFile - copy local.env.example to local.env and fill in your API token."
}
Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)=(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), "Process")
    }
}

Set-Location $repo
New-Item -ItemType Directory -Force -Path "output" | Out-Null

foreach ($offset in 0..2) {
    $date = (Get-Date).ToUniversalTime().AddDays($offset).ToString("yyyy-MM-dd")
    $out  = "output\tee_times_local_$date.json"
    "-- scrape $Platforms for $date" | Tee-Object -FilePath $log -Append
    python -m scraper.aggregate --platforms $Platforms --date $date --out $out 2>&1 |
        Tee-Object -FilePath $log -Append
    if ($LASTEXITCODE -eq 0) {
        python -m scraper.d1 push --data $out 2>&1 | Tee-Object -FilePath $log -Append
    } else {
        "   scrape failed for $date (exit $LASTEXITCODE); skipping push" |
            Tee-Object -FilePath $log -Append
    }
}
"==== done @ $(Get-Date -Format o) ====" | Tee-Object -FilePath $log -Append
