# Aidrobe Backend Handoff

Last updated: 2026-05-01

## Current State

The backend has moved beyond the original V1 Slim MVP skeleton. It now includes production-readiness foundations, validation instrumentation, and extension points for the post-V1 roadmap.

Implemented core API areas:

- `GET /healthz`
- `POST /v1/auth/login/apple`
- `POST /v1/auth/refresh`
- `GET /v1/user/settings`
- `PATCH /v1/user/settings`
- `POST /v1/uploads/presign`
- `POST /v1/import-tasks`
- `GET /v1/import-tasks/{id}`
- `POST /v1/import-tasks/{id}/confirm`
- `GET /v1/wardrobe-items`
- `GET /v1/wardrobe-items/{id}`
- `PATCH /v1/wardrobe-items/{id}`
- `DELETE /v1/wardrobe-items/{id}`
- `GET /v1/recommendations/today`
- `GET /v1/recommendations/outfits/{id}`
- `POST /v1/recommendations/outfits/{id}/wear`
- `POST /v1/recommendations/outfits/{id}/feedback`
- `GET /v1/recommendations/outfit-history`

## Production Foundations

Implemented:

- Apple Sign In verifier and JWT access/refresh tokens.
- PostgreSQL-backed `user_sessions`.
- `Authorization: Bearer` auth in production.
- Development-only `X-User-Id` fallback outside production.
- S3/MinIO-compatible presigned upload adapter.
- Qwen VL vision adapter with fake adapter retained for local development.
- Per-user in-memory Vision rate limiting.
- Generic in-memory API rate limiting.
- Sentry initialization and user binding.
- Dockerfile, Docker Compose, Nginx reverse proxy config.
- Standalone scheduler entry point via `python -m app.jobs.scheduler`.

## Validation Analytics

Implemented `operation_logs` with backend events:

- `user_created`
- `user_login`
- `app_open`
- `item_imported`
- `recommendation_viewed`
- `outfit_worn`
- `feedback_given`
- `item_deleted`

These events support the Phase 2 retention and adoption queries from the roadmap.

## V1.1+ Backend Additions

Implemented:

- APNs provider adapter and user settings fields for `device_token` and `push_enabled`.
- Import input/source extensions for screenshot/share/link use cases.
- Best-effort HTML link parser for extracting a primary image.
- Outfit history endpoint.
- Feedback `reason_tags`.
- Time-sensitivity tagging and API-side filtering by remaining departure time.
- Recommendation scene parameter: `commute`, `formal`, `casual`.
- Wardrobe item `location` field.

## V1.2/V2 Extension Points

Implemented as foundations, not fully productized ML systems:

- LLM port and Qwen LLM adapter.
- Weather port and QWeather adapter.
- Embedding port.
- `item_embeddings` table using JSONB vector storage as a lightweight placeholder.
- Preference snapshot helper.

## Client Companion

The iOS client lives at:

```text
D:\project\Python\aidrobe-frontend
```

It is currently an Expo React Native project rather than a native SwiftUI project. It has been updated to match the current backend routes and DTO shape.

## Verification

Last successful backend verification on 2026-05-01:

```powershell
uv run ruff check .
uv run mypy app
uv run pytest -q
uv run alembic upgrade head
uv run alembic downgrade base
uv run alembic upgrade head
docker compose config --quiet
```

Observed result:

- `ruff check .`: passed
- `mypy app`: passed
- `pytest -q`: 48 passed
- Alembic upgrade/downgrade/upgrade cycle: passed
- Docker Compose config validation: passed

## Operating Notes

- Keep `.env.example` as the committed environment template; do not commit `.env`.
- Full tests require Docker Desktop because testcontainers starts PostgreSQL.
- `X-User-Id` is development-only. Production must use Bearer JWT auth.
- `VISION_PROVIDER=fake` and `STORAGE_PROVIDER=local` are suitable for local development.
- Use `VISION_PROVIDER=qwen` and `STORAGE_PROVIDER=s3` for real provider integration.
