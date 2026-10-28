param(
    [switch]$DryRun,
    [switch]$NoScrape,
    [switch]$Headless,
    [int]$Limit = 0,
    [int]$MaxApplies = -1
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

$cli = @("main.py", "run")
if ($DryRun) { $cli += "--dry-run" }
if ($NoScrape) { $cli += "--no-scrape" }
if ($Headless) { $cli += "--headless" }
if ($Limit -gt 0) { $cli += @("--limit", "$Limit") }
if ($MaxApplies -ge 0) { $cli += @("--max-applies", "$MaxApplies") }

Push-Location $root
try {
    & $python @cli
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
