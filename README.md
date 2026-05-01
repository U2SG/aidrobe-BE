# Aidrobe Backend

Aidrobe is an AI wardrobe and outfit recommendation assistant. This repository currently contains the V1 Slim backend MVP implemented with FastAPI, async SQLAlchemy, Alembic, PostgreSQL, and testcontainers-based tests.

## Current Scope

Implemented backend capabilities:

- Health check: `GET /healthz`
- Unified API envelope and friendly error responses
- Temporary development authentication through `X-User-Id`
- User settings for recommendation time, temperature sensitivity, and style preference
- Image upload presign contract with local fake storage
- Image-only import task creation and confirmation
- Wardrobe item list, update, and delete APIs
- Daily outfit recommendation generation, detail retrieval, wear action, and feedback
- Background recommendation recompute job and optional scheduler wiring

The product scope source of truth is `docs/12_V1范围精简方案.md`. The implemented API contract is documented in `docs/api-v1-slim.md`.

## Architecture

The backend is organized around a thin Clean Architecture style:

- `app/presentation`: FastAPI routers, schemas, dependencies, and exception handling
- `app/application`: use cases, recommendation logic, and port definitions
- `app/infrastructure`: database models/repositories, storage adapter, vision adapter, and logging
- `app/jobs`: background recompute and scheduler setup
- `alembic`: schema migrations
- `tests`: API, service, and unit tests

See `docs/architecture-v1-slim.md` for the implementation map.

## Local Setup

Prerequisites:

- Python 3.12+
- Docker Desktop, required by PostgreSQL testcontainers
- `uv`

Install dependencies:

```powershell
uv sync --group dev
```

Create local environment config:

```powershell
Copy-Item .env.example .env
```

Run the app:

```powershell
uv run fastapi dev app/main.py
```

Run verification:

```powershell
uv run pytest -q
uv run ruff check .
uv run mypy app
```

Migration smoke check:

```powershell
uv run alembic upgrade head
uv run alembic downgrade base
uv run alembic upgrade head
```

More details are in `docs/dev-setup.md`.

## Handoff

The 2026-05-01 backend implementation handoff is in `docs/handoff-v1-slim.md`. Remaining production work includes real authentication, real vision parsing, production object storage, deployment configuration, and the iOS client.
