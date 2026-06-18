# Android real-device debugging via Tailscale.
# Mirrors aidrobe-frontend/scripts/dev-android.ps1 — run both in parallel.
#
# Usage:  .\scripts\dev-android.ps1 [-Port 8000] [-Workers 1]
#   -Port    : uvicorn port (default 8000, must match frontend API_BASE_URL)
#   -Workers : uvicorn worker count (default 1; keep 1 for --reload to work)
#
# Prerequisites:
#   - Tailscale running and connected
#   - PostgreSQL + Redis running (docker compose up -d, or local services)
#   - MinIO / S3-compatible running on port 9000 (for image serving on phone)
#   - uv installed (dependency manager)

param(
    [int]$Port    = 8000,
    [int]$Workers = 1
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot

# 1) Resolve Tailscale IPv4 — same logic as frontend script.
try {
    $tsIp = (tailscale ip -4 | Select-Object -First 1).Trim()
} catch {
    Write-Error 'tailscale CLI not found or not running. Start Tailscale first.'
    exit 1
}
if (-not $tsIp) {
    Write-Error 'Could not resolve Tailscale IPv4. Is Tailscale connected?'
    exit 1
}

# 2) Free the port if a previous uvicorn of THIS project is still on it.
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)" -ErrorAction SilentlyContinue
    if ($proc -and $proc.CommandLine -like "*$projectRoot*") {
        Write-Host "Stopping previous uvicorn (pid $($proc.ProcessId)) on port $Port"
        Stop-Process -Id $proc.ProcessId -Force -Confirm:$false
        Start-Sleep -Seconds 2
    } else {
        Write-Error "Port $Port is taken by another process (pid $($conn.OwningProcess)). Pick another: -Port 8001"
        exit 1
    }
}

# 3) Override only the values that differ between localhost and phone access.
#    Everything else is read from .env as usual.
$env:S3_PUBLIC_BASE_URL = "http://${tsIp}:9000/aidrobe"
# Presigned upload URLs must also be reachable from the phone.
$env:S3_ENDPOINT_URL    = "http://${tsIp}:9000"
# Ensure Python output is UTF-8 (avoid GBK encoding errors on Windows).
$env:PYTHONUTF8           = '1'
$env:PYTHONIOENCODING     = 'utf-8'

Write-Host ''
Write-Host '=============================================='
Write-Host "  Backend API:     http://${tsIp}:${Port}"
Write-Host "  S3 images:       http://${tsIp}:9000/aidrobe"
Write-Host "  Frontend should set API_BASE_URL=http://${tsIp}:${Port}"
Write-Host '=============================================='
Write-Host ''
Write-Host 'Checklist:'
Write-Host '  [?] PostgreSQL running  (psql -h localhost aidrobe)'
Write-Host '  [?] Redis running       (redis-cli ping)'
Write-Host '  [?] MinIO running       (http://localhost:9001  admin console)'
Write-Host '  [?] uv run alembic upgrade head  (if migrations pending)'
Write-Host ''

# 4) Start uvicorn bound to 0.0.0.0 so Tailscale IP is reachable from phone.
Set-Location $projectRoot
uv run uvicorn app.main:app `
    --host 0.0.0.0 `
    --port $Port `
    --workers $Workers `
    --reload
