# Development Setup

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

