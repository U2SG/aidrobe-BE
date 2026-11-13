# Development Setup

## Quick start: zero-cost local stack (macOS / Linux)

Requires Docker (OrbStack or Docker Desktop). No `.env` is needed.

```sh
scripts/dev-up.sh      # postgres+pgvector, redis, minio, migrate, api, worker
scripts/dev-smoke.sh   # presign -> upload -> import -> confirm x2 -> /today
curl localhost:8000/version   # {"git_sha": ..., "alembic_revision": ...}
docker compose -f docker-compose.yml -f docker-compose.local.yml down
```

`docker-compose.local.yml` forces every paid or external provider to its fake /
disabled variant (vision, OCR, LLM, embedding, weather, cutout, receipt import,
analytics) and points storage at the local MinIO, overriding anything in `.env`.
Enable a real provider only deliberately, one at a time, after checking its cost.

To test from a phone, run `AIDROBE_PUBLIC_HOST=<this machine's LAN or Tailscale IP> scripts/dev-up.sh`
so presigned upload and image URLs resolve from the device.

The sections below describe the older host-run (Windows / PowerShell) setup.

## Install

```powershell
uv sync
.\.venv\Scripts\Activate.ps1
```

If uv has global cache permission issues on Windows:

```powershell
$env:UV_CACHE_DIR=(Join-Path (Get-Location) '.uv-cache')
uv sync
```

## Environment

Copy `.env.example` to `.env` and adjust values as needed.

Important variables:

- `APP_ENV=dev`
- `DATABASE_URL=postgresql+asyncpg://...`
- `ENABLE_SCHEDULER=false`
- `VISION_PROVIDER=fake`
- `STORAGE_PROVIDER=local`

## Run

```powershell
make dev
```

## Test

Docker Desktop must be running because tests use PostgreSQL through testcontainers.

```powershell
pytest -q
```

## Migrations

```powershell
alembic upgrade head
alembic downgrade base
alembic upgrade head
```

## Scheduler Notes

Set `ENABLE_SCHEDULER=true` only for a single process. APScheduler must run in one process or a dedicated worker to avoid duplicate recommendation generation.

## Production Auth Note

`X-User-Id` is a development-only authentication shortcut. `APP_ENV=prod` disables it; real auth must be implemented before production deployment.

