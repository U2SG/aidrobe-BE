# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Dependency / environment management uses `uv` (the project's `.venv` is not on `PATH` for naked `python`, so prefix Python invocations with `uv run`).

- Install deps: `uv sync --group dev`
- Run dev server: `uv run fastapi dev app/main.py` (also `make dev`)
- Run all tests: `uv run pytest -q` (also `make test`)
- Run a single test: `uv run pytest tests/api/test_wardrobe.py::test_list_items -q`
- Lint + type check: `uv run ruff check . && uv run mypy app` (also `make lint`)
- Auto-fix + format: `uv run ruff check --fix . && uv run ruff format .` (also `make format`)
- Create migration: `make migrate-create m="message"` → `alembic revision --autogenerate -m "..."`
- Apply migrations: `make migrate-apply` → `alembic upgrade head`
- Migration smoke: `uv run alembic upgrade head && uv run alembic downgrade base && uv run alembic upgrade head`
- Regenerate OpenAPI spec (`docs/openapi.{json,yaml}`): `make openapi` — re-run after any router/schema change so frontend artifacts stay in sync.

Tests use **PostgreSQL via testcontainers**, so Docker Desktop must be running before running the suite.

Pre-handoff checklist after backend changes: `uv run pytest -q`, `uv run ruff check .`, `uv run mypy app`, plus an Alembic upgrade/downgrade/upgrade cycle when migrations change.

## Architecture

Thin Clean Architecture FastAPI service backed by PostgreSQL through async SQLAlchemy. Alembic owns the schema. Layer boundaries are strict — only the layer directly above may import from the one below.

- `app/main.py` — application factory, middleware, router inclusion, lifespan (Sentry init, optional APScheduler, Redis cleanup).
- `app/config/settings.py` — env-driven settings via pydantic-settings; `APP_ENV` switches dev/prod behavior.
- `app/common` — shared enums, error codes, `FriendlyException` (translated by middleware into the unified envelope).
- `app/presentation/api/v1`
  - `routers/` — 12 routers: auth, health, settings, uploads, import_tasks, wardrobe, fit, recommendations, style, insights, outfit_history, outfit_visual.
  - `schemas/` — Pydantic request/response models (one file per domain).
  - `deps.py` — current user, DB session, repository wiring.
- `app/presentation/middlewares` — exception → envelope translation, in-memory rate limit.
- `app/application` — use cases per domain (auth, wardrobe, recommendations, outfits, insights, style, fit_profile, import_tasks, users) + `ports/` for storage/vision/repository interfaces.
- `app/infrastructure` — concrete adapters: `persistence` (SQLAlchemy models + repos), `storage` (S3 + local fake), `vision`, `embedding` (CLIP, gated by `PGVECTOR_ENABLED`), `llm`, `cache` (Redis), `importers`, `workers` (arq), `weather`, `push`, `security`, `logging`.
- `app/jobs` — APScheduler wiring (off by default; `ENABLE_SCHEDULER=false`) + arq worker entrypoints.
- `alembic/` — migration history; `tests/` mirrors layers (`api/`, `services/`, `unit/`).

### Request flow

Router → Pydantic validation → `deps.py` resolves user/session/services → application use case (raises `FriendlyException` for expected user-facing errors) → infrastructure repository → unified response envelope `{code, message, data}`.

### Known API conventions and inconsistencies (matter for frontend work)

- **Auth**: `APP_ENV=dev` accepts `X-User-Id: <uuid>` shortcut; `APP_ENV=prod` requires `Authorization: Bearer <jwt>` and must reject `X-User-Id`.
- **Response envelope**: V1 endpoints wrap responses in `{code, message, data}`. **V3 endpoints (insights, outfit-history, outfit-visual) return data directly** — this is intentional, not a bug.
- **Pagination**: `wardrobe` uses `limit/offset`; `outfit-history` uses `page/page_size`. Documented in `docs/ios-api-alignment.md`; do not "fix" piecemeal.
- **UUID id field naming differs per resource** (`item_id`, `outfit_id`, `wear_history_id`, `asset_id`) — intentional, see ios-api-alignment doc.
- **Errors**: `FriendlyException` → `{error_code, message}`. Codes: 4001 validation / 4002 auth / 4003 forbidden / 4041 not found / 4091 conflict / 5001 internal.

### V3 surface notes

- Outfit board generation (`/v1/outfit-visual-assets`) is async via the **arq** worker — clients poll `generation_status`.
- `pgvector` / CLIP embeddings only activate when `PGVECTOR_ENABLED=true`; tests fall back to JSONB so the suite runs without the extension.
- `wear_count` increments on `POST /v1/recommendations/outfits/{id}/wear`; legacy rows need a backfill before prod.

## Project conventions

- Source-of-truth product scope: `docs/12_V1范围精简方案.md`. iOS contract: `docs/ios-api-alignment.md`. Slim API ref: `docs/api-v1-slim.md`. Architecture map: `docs/architecture-v1-slim.md`.
- Use `.env.example` as the documented config template; never commit `.env`.
- mypy runs in `strict` mode for the `app` package — keep new code typed.
- Ruff lint set: `E, F, I, B, UP` (line length 120, py312). `UP046` is ignored repo-wide; alembic versions ignore `E501`.
- Pytest is `asyncio_mode = "auto"` with session-scoped event loop — async tests do not need `@pytest.mark.asyncio`.
