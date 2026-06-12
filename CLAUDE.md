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

- `app/main.py` — application factory, middleware (CorrelationId + rate-limit + timing), router inclusion, lifespan (Sentry init, optional APScheduler, Redis cleanup).
- `app/config/settings.py` — env-driven settings via pydantic-settings; `APP_ENV` switches dev/prod behavior. Phase B/D/E features each have a dedicated `ENABLE_*` flag (default off).
- `app/common` — shared enums, error codes, `FriendlyException` (translated by middleware into the unified envelope), i18n helpers, season helpers.
- `app/presentation/api/v1`
  - `routers/` — 14 routers: auth, account, admin, health, settings, uploads, import_tasks, wardrobe, fit, recommendations, style, insights, outfit_history, outfit_visual.
  - `schemas/` — Pydantic request/response models (one file per domain).
  - `deps.py` — current user (with `deactivated_at` gate), `require_admin_user`, DB session, repository wiring.
- `app/presentation/middlewares` — `correlation.py` (X-Correlation-Id propagation), `timing.py`, exception → envelope translation, in-memory rate limit.
- `app/application` — use cases per domain (auth, wardrobe, recommendations, outfits, insights, style, fit_profile, import_tasks, users) + `ports/` for storage / vision / sts_token / weather / llm / embedding interfaces. Also: `audit.py` (audit-log helper), `cost_guard.py` (per-user-per-day quota).
- `app/infrastructure`
  - `persistence` — SQLAlchemy models + repos.
  - `storage` — `local_storage` / `s3_storage` / `aliyun_oss` (Phase E5) + `aliyun_sts` / `fake_sts` + `lifecycle` (retention.yaml loader) + `cdn_url` (Type A signing).
  - `workers` — `job_runtime` (Phase C foundation) + `embedding_worker` / `outfit_board_worker` / `user_purge_worker` drainers + `worker_main` arq entrypoint registry + the legacy import / image / style_learn workers.
  - `vision`, `embedding` (CLIP, gated by `PGVECTOR_ENABLED`), `llm` (with `sanitizer.py` for input whitelist + HMAC user token), `cache` (Redis), `importers`, `weather`, `push`, `security`, `logging`.
- `app/jobs` — APScheduler wiring (off by default; `ENABLE_SCHEDULER=false`) + arq worker entrypoints.
- `alembic/` — migration history (currently 0026); `tests/` mirrors layers (`api/`, `services/`, `unit/`).

### Request flow

Router → CorrelationIdMiddleware binds correlation_id → Pydantic validation → `deps.py` resolves user (rejecting deactivated) / session / services → application use case (raises `FriendlyException` for expected user-facing errors) → infrastructure repository → unified response envelope `{code, message, data}`.

### Async-jobs runtime (Phase C)

Single contract for retryable / cost-bearing async work, in `app/infrastructure/workers/job_runtime.py`:

- `enqueue(...)` is idempotent on `idempotency_key`; duplicate keys return the existing row, no double-charge.
- `claim_one(...)` uses `FOR UPDATE SKIP LOCKED` and **commits before returning** so a worker crash can't roll the claim back. Sets `lease_expires_at = now + 10 min`.
- `mark_succeeded` / `mark_failed` are fenced on `WHERE id=:id AND status='running' AND attempts=:claimed_attempt`. Workers pass `claimed_attempt=job.attempts` so a stale finalizer after lease recovery + reclaim returns False instead of overwriting the new claim.
- `recover_expired_leases(...)` runs as a cron sweep. Two-phase: rows with `attempts >= max_attempts` go straight to `dead`; the rest go back to `queued`. Without the dead-letter branch, a crash-loop never paged oncall.
- `_alert_dead(...)` captures a Sentry event inside `push_scope` (tags + extras attached BEFORE `capture_message` fires) for both dead-letter paths.

Idempotency-key conventions per `job_type` are enumerated in `docs/async-jobs.md`. Live workers: `embedding`, `outfit_board`, `user_purge`. Reserved for V4: `llm_ask`, `ocr_receipt`, `pdf_ingest`, `tryon`, `user_export`.

### Object storage (Phase E5)

Three providers selected via `STORAGE_PROVIDER`:

- `local` — dev only.
- `s3` — AWS / S3-compatible.
- `aliyun_oss` — China prod. Pairs with `POST /v1/uploads/sts-token` (`ENABLE_OSS_STS`) for client-direct uploads scoped to `users/<user_id>/<action>/`.

`ObjectStoragePort.sign_render_url(key, ttl, process)` is what reads call to materialize a short-lived URL at request time. Internal-storage rows persist `object_key` (stable) instead of `image_url` (TTL-bound). Read paths sign on demand.

Bucket retention spec lives in `app/config/retention.yaml`; `app/infrastructure/storage/lifecycle.py` parses it and renders OSS-SDK rules. The four buckets per plan §2.9:

- `aidrobe-items` — wardrobe item cutouts/originals (user content, no auto-purge)
- `aidrobe-ootd` — OOTD selfies (user-deletable)
- `aidrobe-private` — try-on body images (auto-purge 30d, PIPL-aligned)
- `aidrobe-derived` — outfit board renders (60d expiry; cheap to recreate)

### PIPL soft delete (Phase E1)

`User` rows carry `deactivated_at` and `purged_at`. `DELETE /v1/me` (`ENABLE_USER_PURGE`) stamps `deactivated_at`, revokes all sessions, and enqueues a `user_purge` async job at `deactivated_at + USER_PURGE_GRACE_DAYS` (default 30). The drainer cascades user delete via FK ON DELETE CASCADE. `audit_log` has NO FK to users on purpose — DSAR responses must outlive the purge.

Apple-login auto-reactivates a soft-deleted user during grace (`AuthService.login_with_apple` clears `deactivated_at` if `purged_at IS NULL`); the dormant purge job detects this on fire and writes `user.purge_cancelled` audit instead of deleting.

### Cost guard (Phase E3)

`app/application/cost_guard.py` aggregates `async_jobs.cost_*` per `(user_id, job_type, today)`. Two modes:

- Token quota (`llm_ask`): SUM(cost_tokens) over status IN (succeeded, running, failed, dead) — provider partial completions are real cost.
- Call-count quota (`ocr_receipt`, `tryon`): COUNT(*) over status IN (succeeded, running) only — failed retries don't burn quota.

When `ENABLE_COST_GUARD=true`, `assert_within_quota` accepts an optional Redis client for atomic INCRBY-then-rollback admission control (race-safe). Without Redis it falls back to the DB-aggregate path (TOCTOU-leaky but acceptable for low concurrency). DB stays the audit ground truth either way.

### LLM input sanitizer (Phase E4)

Anything heading into an LLM prompt MUST go through `app/infrastructure/llm/sanitizer.py`. Whitelist per plan §2.9 (category/color/status/location/season_tags/fit_*); raw user_id replaced by an opaque `usr_<22-char>` HMAC-SHA256 token derived from `LLM_USER_TOKEN_HMAC_KEY`. Prod startup hard-fails if the key is still the placeholder.

### Known API conventions and inconsistencies (matter for frontend work)

- **Auth**: `APP_ENV=dev` accepts `X-User-Id: <uuid>` shortcut; `APP_ENV=prod` requires `Authorization: Bearer <jwt>` and must reject `X-User-Id`. Both paths reject deactivated users with 401 "Account deactivated".
- **Response envelope**: V1 endpoints wrap responses in `{code, message, data}`. **V3 endpoints (insights, outfit-history, outfit-visual) return data directly** — this is intentional, not a bug.
- **Pagination**: `wardrobe` uses `limit/offset`; `outfit-history` uses `page/page_size`. Documented in `docs/ios-api-alignment.md`; do not "fix" piecemeal.
- **UUID id field naming differs per resource** (`item_id`, `outfit_id`, `wear_history_id`, `asset_id`) — intentional, see ios-api-alignment doc.
- **Errors**: `FriendlyException` → `{error_code, message}`. Codes: 4001 validation / 4002 auth / 4003 forbidden (or 503 kill-switch) / 4041 not found / 4091 conflict (or 429 rate-limit) / 5001 internal.
- **`image_url` lifetime**: when the source is internal storage on a private bucket, `image_url` is a signed URL with a 30-min TTL. Clients must NOT persist past the session boundary. External (import-flow) URLs are passed through as-is.

### V3+ surface notes

- Outfit board generation (`/v1/outfit-visual-assets`) is async via `outfit_board_worker` (job_runtime) — clients poll `generation_status`. Dead jobs get the asset row marked `failed` by `reconcile_dead_outfit_board_assets` cron.
- `pgvector` / CLIP embeddings only activate when `PGVECTOR_ENABLED=true`; tests fall back to JSONB so the suite runs without the extension. Embedding worker fans replacement candidates via `find_replacements_batch` when pgvector is off (one IN-query, in-memory rank).
- `daily_recommendations.revision`: `POST /v1/recommendations/today/refresh` (`ENABLE_TODAY_REFRESH`) writes a new revision; old revision rows + their wear/feedback FK references survive forever. Refresh races serialize via `pg_advisory_xact_lock(user_id, date)`.

## Test infrastructure

- `tests/conftest.py` includes a `_truncate_between_tests` autouse fixture that TRUNCATEs all tables before each test. This is necessary because Phase C+ workers commit via `AsyncSessionLocal` (a separate engine) — those commits leak past the per-test transaction rollback otherwise.
- Tests that flip a feature flag (e.g. `ENABLE_TODAY_CONTEXT`) MUST monkeypatch `get_settings` on the *consuming* module (not just the FastAPI dep override), because `get_settings` is `lru_cached` and bypasses `app.dependency_overrides`. See `tests/services/test_cost_guard.py` and `tests/api/test_today_context.py` for the pattern.
- When seeding parent + child rows (e.g. `User` + `OutfitVisualAsset`), `await session.flush()` between them. SQLAlchemy unitofwork doesn't always order INSERTs by FK dependency reliably when there's no Python-level relationship.
- Handlers should NOT call `await session.commit()` explicitly — the `get_db_session` dependency commits on yield-exit. An explicit commit inside a handler can detach lazy relationships and trip `MissingGreenlet` when Pydantic walks them during serialization.
- For endpoints that return data freshly written (e.g. refresh creates a new daily), re-fetch via the repo's `get_daily(...)` so eager-loaded relationships (outfits + items) come along — direct return of the just-inserted ORM object will lazy-load on serialization.

## Project conventions

- Source-of-truth product scope: `docs/12_V1范围精简方案.md`. V3+ iOS contract: `docs/ios-api-v3plus-update.md` (delta from `docs/ios-api-alignment.md`). Async-jobs framework reference: `docs/async-jobs.md`. Wardrobe state machine: `docs/wardrobe-status-machine.md`. Architecture map: `docs/architecture-v1-slim.md`.
- Use `.env.example` as the documented config template; never commit `.env`.
- mypy runs in `strict` mode for the `app` package — keep new code typed.
- Ruff lint set: `E, F, I, B, UP` (line length 120, py312). `UP046` is ignored repo-wide; alembic versions ignore `E501`.
- Pytest is `asyncio_mode = "auto"` with session-scoped event loop — async tests do not need `@pytest.mark.asyncio`.
- New feature flags default to `False`. The convention: persist data regardless of the flag (don't lose it on rollback) but gate exposure behind the flag — that way flipping back to `false` is a clean rollback.
- Migration filenames are 4-digit-prefixed (`0026_...`); ID matches filename. `alembic/env.py` widens `alembic_version.version_num` to VARCHAR(128) on connect because the long IDs exceed the default 32-char column.

---

## Worktree 并行工作流

多个 Claude session 并行干活，每个 worktree = 独立目录 + 独立分支，互不干扰；每个 session 出一个 PR。

### 粒度判断（标准：能否独立 review/merge）

- 新功能 / 独立模块 → 一个 worktree 一个分支一个 PR
- 测试期零散 bugfix → 全部塞同一个 worktree，打包一个 PR
- 同一任务多步骤拆解 → 一个 worktree 走完

### Windows 标准流程

```powershell
# 1) 主目录开 worktree（分支名语义化，不带 Jira 编号）
cd D:\project\<this-repo>
claude -w <branch-name>          # 例：claude -w oauth-token-rotation
# 自动建 .claude\worktrees\<branch>\ + 同名分支 + 启动 session

# 2) session 里改完，一键 commit + push + 建 PR
/commit-commands:commit-push-pr  # 走完 commit → git push → tea pr create

# 3) 退出但保留 worktree（等 review），主目录可继续 claude -w 跑下一个

# 4) Review 期间改动：恢复 session
cd D:\project\<this-repo>\.claude\worktrees\<branch>
claude --resume                  # 完整上下文恢复，不用重述背景

# 5) Merge 后清理
git worktree remove .claude\worktrees\<branch>
git branch -d <branch>
# 或：/clean_gone 一键清所有 [gone] 分支
```

### 注意

- **分支名语义化，不带 Jira 编号**（用 `oauth-token-rotation` 而非 `feat-fms-1234-oauth`）
- 并行 3–5 个为宜，开多了自己 review 不过来
- 主目录只作入口，所有改动都在 `.claude\worktrees\<name>\` 子目录内完成
- 多 worktree 共享同一份 `.git`，磁盘成本可忽略
- `CLAUDE.md` / `.claudeignore` 自动继承主分支当前版本
- 每个 worktree 可独立配对 Happy，手机端显示为多个独立项目卡片
