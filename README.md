# Aidrobe Backend

Aidrobe is an AI wardrobe and outfit recommendation assistant. The repository
holds the FastAPI / async-SQLAlchemy / Alembic / PostgreSQL backend, with
testcontainers-driven integration tests.

## Status — V3+ (Phases A → E shipped)

The codebase has progressed past the V1 Slim MVP through five phases of
production hardening. All changes are additive on the API contract, gated
by feature flags where rollout risk warranted it.

| Phase | Theme | Highlights |
|---|---|---|
| **A** | Contract + Consistency | 4-tier feedback, `feedback/bundle`, `wear-history`, `daily_recommendations.revision`, `last_worn_at` denormalization, status-machine doc |
| **B** | Explainability Runtime | `OutfitResponse.scores / obstacles / prep_steps`, i18n contract `{key, params, fallback_text}` |
| **C** | Async Reliability Framework | `async_jobs` table + `job_runtime` (claim_one + lease + dead-letter), embedding & outfit_board workers on top of it, Sentry `job.dead` alerts, `/v1/admin/jobs` |
| **D** | Context + Personalization | `daily_recommendations.context_json` (weather + scene), `POST /today/refresh` with revisioning + advisory-lock concurrency, replacement-candidate cascade, `weather_mismatch` obstacle |
| **E** | Privacy + Cost Guardrails | PIPL-aware `DELETE /v1/me` (soft delete + 30d grace + auto-reactivation on Apple re-login), Aliyun OSS adapter + STS token + retention policy, LLM input sanitizer with HMAC user token, daily cost quotas |

Source-of-truth docs:
- Plan + retrospective: [`docs/backend-gaps-plan.md`](docs/backend-gaps-plan.md)
- iOS integration (V3+ delta): [`docs/ios-api-v3plus-update.md`](docs/ios-api-v3plus-update.md)
- Original V3 contract: [`docs/ios-api-alignment.md`](docs/ios-api-alignment.md)
- Async-jobs framework: [`docs/async-jobs.md`](docs/async-jobs.md)
- Wardrobe status machine: [`docs/wardrobe-status-machine.md`](docs/wardrobe-status-machine.md)
- Bucket retention spec: [`app/config/retention.yaml`](app/config/retention.yaml)
- Generated OpenAPI: [`docs/openapi.yaml`](docs/openapi.yaml)

## Architecture

Thin Clean Architecture; layer boundaries enforced (only the layer
directly above may import from the one below).

- `app/main.py` — application factory, middleware (CorrelationId, rate-limit), router inclusion, lifespan
- `app/config/settings.py` — env-driven settings; flags gate every Phase B/D/E feature
- `app/common` — shared enums, error codes, FriendlyException → unified envelope, i18n helpers
- `app/presentation/api/v1` — 14 routers (auth, account, wardrobe, fit, recommendations, style, insights, outfit_history, outfit_visual, uploads, import_tasks, admin, settings, health) + Pydantic schemas + deps
- `app/application` — use cases per domain, port protocols (storage / vision / LLM / weather / sts / embedding)
- `app/infrastructure` — concrete adapters: persistence (SQLAlchemy + Alembic), storage (local / S3 / Aliyun OSS), workers (arq runner + job_runtime drainers), vision, embedding (CLIP), LLM, cache (Redis), security, logging
- `app/jobs` — APScheduler wiring + arq entrypoints
- `alembic/` — migrations; tests mirror layer structure

### Async-jobs runtime (Phase C)

All retryable / cost-bearing work goes through `app/infrastructure/workers/job_runtime.py`:

- `enqueue` is idempotent on `idempotency_key`
- `claim_one` is `FOR UPDATE SKIP LOCKED` + commit-on-claim with a 10-min lease, so a worker crash can't roll back the claim
- `mark_succeeded` / `mark_failed` are fenced on `(claimed_attempt, status='running')` — no lost updates from stale workers after lease recovery
- `recover_expired_leases` dead-letters jobs whose attempts hit `max_attempts`; otherwise re-queues with backoff (60s / 5m / 30m / 2h)
- `job.dead` events fire a Sentry alert with the right tags

Jobs in flight: `embedding`, `outfit_board`, `user_purge`. Reserved for V4: `llm_ask`, `ocr_receipt`, `pdf_ingest`, `tryon`, `user_export`.

### Object storage (Phase E5)

Three providers selected via `STORAGE_PROVIDER`:

- `local` — dev only
- `s3` — AWS / S3-compatible
- `aliyun_oss` — China prod; pairs with `POST /v1/uploads/sts-token` for client-direct uploads (clients hold short-lived STS tokens scoped to `users/<user_id>/<action>/`, never permanent AccessKeys)

Plan §2.9 mandates four buckets with distinct retention policies. The lifecycle spec lives in [`app/config/retention.yaml`](app/config/retention.yaml) and is applied to OSS via the SDK out-of-band.

`image_url` returned by API endpoints is signed at request time when the source is internal storage — clients must NOT persist it past the session boundary (default TTL 30 min).

## Local Setup

Prerequisites:

- Python 3.12+
- Docker Desktop (required by testcontainers)
- `uv`

```powershell
# Install deps
uv sync --group dev

# Create local config
Copy-Item .env.example .env

# Run the app
uv run fastapi dev app/main.py

# Or via Make (more shorthand commands)
make dev
```

## Verification

```powershell
# All tests (282+ at last count, all green)
uv run pytest -q

# Lint + type check
uv run ruff check .
uv run mypy app

# Migration round-trip
uv run alembic upgrade head
uv run alembic downgrade base
uv run alembic upgrade head

# Regenerate OpenAPI when routers/schemas change
make openapi
```

The full pre-handoff checklist is `make ci` (runs lint + mypy + pytest in
order). Tests use PostgreSQL via testcontainers; Docker Desktop must be
running.

## Common feature flags (during rollout)

When promoting a phase to a real deployment, flip these in order:

```bash
# Phase B
ENABLE_OUTFIT_SCORES=true
ENABLE_OBSTACLES=true
ENABLE_PREP_STEPS=true

# Phase D
ENABLE_TODAY_CONTEXT=true
ENABLE_TODAY_REFRESH=true

# Phase E (only after iOS implements the corresponding flows)
ENABLE_USER_PURGE=true       # DELETE /v1/me
ENABLE_OSS_STS=true          # POST /v1/uploads/sts-token
ENABLE_COST_GUARD=true       # quota enforcement on cost-bearing jobs
```

Each flag is a kill switch — flipping back to `false` rolls the feature
back without losing already-persisted data (e.g. `context_json` keeps
being written even when `ENABLE_TODAY_CONTEXT=false`; the flag only
gates response exposure).

See [import pipeline rollout](docs/import-pipeline-rollout.md) for the API and worker upgrade order.

## Handoff

V3+ rolling handoff lives in:
- [`docs/ios-api-v3plus-update.md`](docs/ios-api-v3plus-update.md) — full delta vs the original V3 contract, with a prioritized iOS migration checklist
- [`docs/backend-gaps-plan.md`](docs/backend-gaps-plan.md) — phase-by-phase plan, retrospective, and V4 candidate work

Outstanding before V4 launch (LLM Ask / OCR / try-on / PDF):
- Real Aliyun OSS bucket provisioning + lifecycle apply
- Apply STS role + RAM trust policy
- Wire LLM provider with the cost-guard + sanitizer pipeline
- iOS implementation of the surfaces in §11 of `ios-api-v3plus-update.md`
