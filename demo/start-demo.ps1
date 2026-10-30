$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$env:FUTURESTACK_DATABASE = Join-Path $PSScriptRoot "data\futurestack-demo.sqlite3"
$env:FUTURESTACK_BRAND_RULES = Join-Path $PSScriptRoot "brand_rules.json"
$env:ENVIRONMENT = "development"
$env:ALLOW_REGISTRATION = "true"
$env:INITIAL_ADMIN_EMAIL = "demo-admin@everpine.local"

if (-not $env:INITIAL_ADMIN_PASSWORD) {
    $env:INITIAL_ADMIN_PASSWORD = Read-Host "Enter a temporary demo admin password (12+ characters)"
}
if ($env:INITIAL_ADMIN_PASSWORD.Length -lt 12) {
    throw "The demo admin password must contain at least 12 characters."
}

Set-Location $projectRoot
python -m uvicorn backend.main:app --reload
