<#
.SYNOPSIS  Convenience launcher for the EGX Market Data API (Windows 11 / PowerShell).
.EXAMPLE   .\run.ps1 -Setup       # create .venv, install dependencies, create .env
.EXAMPLE   .\run.ps1              # start the API on http://127.0.0.1:8000  (Ctrl+C to stop)
.EXAMPLE   .\run.ps1 -Discover    # read-only look at the page's controls (scripts\discover_ui.py)
.EXAMPLE   .\run.ps1 -Diagnose    # real-source diagnostic (scripts\test_source.py)
.EXAMPLE   .\run.ps1 -Once        # one real collection into the local database
.EXAMPLE   .\run.ps1 -Test        # unit tests (mocked HTTP)
.EXAMPLE   .\run.ps1 -Integration # REAL-source integration test
#>
param(
    [switch]$Setup, [switch]$ProbeLoad, [switch]$Discover, [switch]$Diagnose, [switch]$Once, [switch]$Test, [switch]$Integration,
    [string]$BindHost = "127.0.0.1", [int]$Port = 8000
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if ($Setup -or -not (Test-Path $venvPython)) {
    Write-Host "Creating virtual environment and installing dependencies..."
    python -m venv .venv
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r requirements.txt
    & $venvPython -m playwright install chromium
    if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "Created .env from .env.example" }
    if ($Setup) { Write-Host "Setup complete."; return }
}

if ($ProbeLoad)   { & $venvPython scripts\probe_load.py; exit $LASTEXITCODE }
if ($Discover)    { & $venvPython scripts\discover_ui.py; exit $LASTEXITCODE }
if ($Diagnose)    { & $venvPython scripts\test_source.py; exit $LASTEXITCODE }
if ($Once)        { & $venvPython scripts\run_collector_once.py; exit $LASTEXITCODE }
if ($Test)        { & $venvPython -m pytest -v; exit $LASTEXITCODE }
if ($Integration) { $env:EGX_RUN_INTEGRATION = "1"; & $venvPython -m pytest tests\integration -v -s; exit $LASTEXITCODE }

# ONE worker on purpose: the poller runs inside the API process (see README, "Polling").
Write-Host "Starting EGX API on http://${BindHost}:${Port}  (Swagger UI: http://${BindHost}:${Port}/docs)  Ctrl+C to stop."
& $venvPython -m uvicorn app.main:app --host $BindHost --port $Port --workers 1
