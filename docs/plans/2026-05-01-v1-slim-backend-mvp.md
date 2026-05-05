# V1 Slim Backend MVP Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the V1 Slim backend loop for Aidrobe: image import, wardrobe management, cached daily recommendations, wear selection, and simple feedback.

**Architecture:** Clean Architecture with three layers (application, infrastructure, presentation). Single FastAPI service with domain logic in framework-agnostic Python modules. Use PostgreSQL in all environments (including tests via testcontainers). Avoid queues, pgvector, LLM copy generation, link import, push notifications, and complex preference learning in V1 Slim.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (async), Alembic, Pydantic v2, pytest, HTTPX test client, APScheduler, S3-compatible object storage presign adapter, testcontainers-python, structlog.

---

## Source Documents

- `docs/12_V1范围精简方案.md` is the source of truth for V1 Slim scope.
- `docs/03_整体PRD_MVP.md` is reference only where it does not conflict with V1 Slim.
- `docs/08_API接口草案.md` is reference only; implement the Slim subset.
- `docs/09_数据表结构设计.md` is reference only; keep fields minimal and reserve expansion without implementing unused behavior.
- `docs/10_推荐服务流程图_文字版.md` explains the recommendation pipeline, but V1 Slim uses the simplified rule set.
- `docs/11_已识别风险与待决策点.md` highlights risks; apply the "open app, no empty loading screen" constraint.

## Project Structure

```
aidrobe/
├── app/
│   ├── __init__.py
│   ├── main.py                    # create_app factory, lifespan, middleware chain
│   ├── bootstrap/                 # Dependency wiring
│   │   ├── __init__.py
│   │   └── container.py           # Factory functions for use cases and services
│   ├── config/                    # Configuration
│   │   ├── __init__.py
│   │   └── settings.py            # Pydantic Settings with env fallback
│   ├── common/                    # Shared kernel
│   │   ├── __init__.py
│   │   ├── error_codes.py         # Centralized error code constants
│   │   ├── exceptions.py          # FriendlyException base
│   │   └── enums.py               # All V1 Slim enums
│   ├── application/               # Use cases & ports (framework-agnostic)
│   │   ├── __init__.py
│   │   ├── ports/                 # Abstract interfaces (Protocols)
│   │   │   ├── __init__.py
│   │   │   ├── storage.py         # ObjectStoragePort
│   │   │   ├── vision.py          # VisionPort
│   │   │   └── repositories.py    # Repository protocols per aggregate
│   │   ├── import_tasks/          # Import use cases
│   │   │   ├── __init__.py
│   │   │   ├── create_import.py
│   │   │   └── confirm_import.py
│   │   ├── wardrobe/              # Wardrobe use cases
│   │   │   ├── __init__.py
│   │   │   ├── list_items.py
│   │   │   ├── update_item.py
│   │   │   └── delete_item.py
│   │   ├── recommendations/       # Recommendation use cases
│   │   │   ├── __init__.py
│   │   │   ├── engine.py          # Rule-based engine (pure logic)
│   │   │   ├── colors.py          # Color conflict table
│   │   │   ├── generator.py       # Orchestrates engine + persistence
│   │   │   └── get_today.py       # Today recommendation use case
│   │   ├── outfits/               # Outfit action use cases
│   │   │   ├── __init__.py
│   │   │   ├── wear.py
│   │   │   └── feedback.py
│   │   └── users/                 # User & settings use cases
│   │       ├── __init__.py
│   │       └── settings.py
│   ├── infrastructure/            # Adapters (framework-dependent)
│   │   ├── __init__.py
│   │   ├── persistence/
│   │   │   ├── __init__.py
│   │   │   ├── models/            # SQLAlchemy ORM models
│   │   │   │   ├── __init__.py
│   │   │   │   ├── base.py        # DeclarativeBase, common mixins
│   │   │   │   ├── user.py
│   │   │   │   ├── wardrobe.py
│   │   │   │   ├── import_task.py
│   │   │   │   └── recommendation.py
│   │   │   ├── repositories/      # Repository implementations
│   │   │   │   ├── __init__.py
│   │   │   │   ├── wardrobe_repo.py
│   │   │   │   ├── import_task_repo.py
│   │   │   │   ├── recommendation_repo.py
│   │   │   │   └── user_repo.py
│   │   │   └── session.py         # async_sessionmaker + get_db_session dependency
│   │   ├── storage/               # Object storage adapter
│   │   │   ├── __init__.py
│   │   │   └── local_storage.py   # Dev/test presign adapter
│   │   ├── vision/                # Vision API adapter
│   │   │   ├── __init__.py
│   │   │   └── fake_vision.py     # Test/dev fake
│   │   └── logging/               # Structured logging setup
│   │       ├── __init__.py
│   │       └── setup.py           # structlog JSON configuration
│   ├── presentation/              # API layer
│   │   ├── __init__.py
│   │   ├── api/
│   │   │   ├── __init__.py
│   │   │   └── v1/
│   │   │       ├── __init__.py
│   │   │       ├── deps.py        # get_current_user, get_db_session dependencies
│   │   │       ├── schemas/       # Pydantic request/response DTOs
│   │   │       │   ├── __init__.py
│   │   │       │   ├── common.py  # ResponseModel wrapper
│   │   │       │   ├── settings.py
│   │   │       │   ├── uploads.py
│   │   │       │   ├── import_tasks.py
│   │   │       │   ├── wardrobe.py
│   │   │       │   └── recommendations.py
│   │   │       └── routers/       # Thin route handlers
│   │   │           ├── __init__.py
│   │   │           ├── health.py
│   │   │           ├── settings.py
│   │   │           ├── uploads.py
│   │   │           ├── import_tasks.py
│   │   │           ├── wardrobe.py
│   │   │           └── recommendations.py
│   │   └── middlewares/           # Middleware chain
│   │       ├── __init__.py
│   │       └── exception_handler.py  # FriendlyException → JSON response
│   └── jobs/                      # Background jobs
│       ├── __init__.py
│       ├── scheduler.py           # APScheduler setup
│       └── recommendation_jobs.py # Cron + mutation-triggered recompute
├── alembic/
│   ├── env.py
│   └── versions/
├── tests/
│   ├── conftest.py               # testcontainers PostgreSQL + async fixtures
│   ├── unit/                     # Pure logic tests (engine, colors)
│   │   └── ...
│   ├── api/                      # API integration tests
│   │   └── ...
│   └── services/                 # Use case tests
│       └── ...
├── alembic.ini
├── pyproject.toml
├── Makefile
├── Dockerfile
├── .env.example
└── README.md
```

## Architecture Principles

1. **Clean Architecture layers**: `presentation` → `application` → `infrastructure`. Dependencies point inward. Use cases depend on protocols, not concrete implementations.
2. **Ports & Adapters**: External services (vision API, object storage, database) are accessed through Protocol interfaces defined in `application/ports/`. Infrastructure provides concrete adapters.
3. **Thin route handlers**: Routers in `presentation/` only validate input, call use cases, and return responses. No business logic in routes.
4. **Bootstrap wiring**: `bootstrap/container.py` provides factory functions that wire ports to adapters. `main.py` calls these during app startup via the lifespan context manager.
5. **Unified error handling**: `FriendlyException` carries error code + HTTP status. `ExceptionHandlerMiddleware` catches all exceptions and returns consistent JSON responses.
6. **Structured logging**: `structlog` configured at startup with JSON output. Key paths (vision calls, recommendation generation, scheduler triggers) emit structured log events.
7. **Test isolation**: All tests run against real PostgreSQL via testcontainers. Each test uses a transaction that rolls back after completion.

## Scope

### In Scope

- FastAPI backend skeleton with Clean Architecture.
- Unified error handling middleware and response format.
- Presigned upload endpoint with a local/dev adapter.
- Image-only import tasks.
- Synchronous managed-vision abstraction that returns `category` and `color_main`.
- Confirm import into wardrobe items.
- Wardrobe list (with pagination) / detail / update / delete.
- User settings with `departure_time`.
- Rule-based daily recommendation generation.
- `GET /v1/recommendations/today` returns cached recommendation first and never blocks on generation.
- Wear selection and like/dislike feedback.
- Background recomputation trigger (with debounce) after wardrobe mutations.
- APScheduler jobs for `departure_time - 90 minutes` and 03:00 fallback.
- Structured logging for key paths.

### Out of Scope

- iOS app implementation.
- Link/share/screenshot import.
- Push notifications.
- Weather, scene quick switch, time-richness candidate pools. (Note: scene quick switch was considered for V1 per `docs/11` risk §2.5 but deferred as a conscious scope decision — revisit if early user feedback demands it.)
- LLM recommendation copy.
- Embedding, pgvector, external source parsing.
- Complex auth; use a temporary `X-User-Id` dependency (dev-only, guarded by environment check) until real auth exists.
- Payment, community, virtual try-on, ecommerce.

## Milestones

1. Project scaffold, test harness, and error handling infrastructure.
2. Database models, migrations, and session management.
3. Import and wardrobe CRUD.
4. Recommendation engine and API.
5. Background generation hooks and scheduler.
6. Integration tests and API contract docs.

---

### Task 1: Project Scaffold and Error Infrastructure

**Files:**
- Create: `pyproject.toml`
- Create: `Makefile`
- Create: `.env.example`
- Create: `app/__init__.py`
- Create: `app/main.py`
- Create: `app/config/settings.py`
- Create: `app/common/error_codes.py`
- Create: `app/common/exceptions.py`
- Create: `app/presentation/middlewares/exception_handler.py`
- Create: `app/presentation/api/v1/schemas/common.py`
- Create: `app/presentation/api/v1/routers/health.py`
- Create: `app/infrastructure/logging/setup.py`
- Create: `tests/conftest.py`
- Create: `tests/api/test_health.py`

**Step 1: Write the failing health test**

Create `tests/api/test_health.py`:

```python
from httpx import AsyncClient


async def test_health_check(async_client: AsyncClient) -> None:
    response = await async_client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

**Step 2: Add project metadata and tooling**

Create `pyproject.toml` with FastAPI, SQLAlchemy[asyncio], asyncpg, Alembic, Pydantic settings, pytest, pytest-asyncio, httpx, ruff, mypy, structlog, testcontainers[postgres], and APScheduler. Keep dependencies minimal; do not add Celery, Redis, pgvector, or cloud SDKs yet.

Create `Makefile` with targets:
- `install` → `uv sync`
- `dev` → `fastapi dev app/main.py`
- `test` → `pytest`
- `lint` → `ruff check . && mypy app`
- `format` → `ruff check --fix . && ruff format .`
- `migrate-create` → `alembic revision --autogenerate -m "$(m)"`
- `migrate-apply` → `alembic upgrade head`

**Step 3: Implement configuration**

Create `app/config/settings.py` using Pydantic `BaseSettings` with:
- `DATABASE_URL`: PostgreSQL connection string
- `APP_ENV`: `dev` / `test` / `prod` (default: `dev`)
- `ENABLE_SCHEDULER`: bool (default: `false`)
- `VISION_PROVIDER`: str (default: `fake`)
- `STORAGE_PROVIDER`: str (default: `local`)
- `LOG_LEVEL`: str (default: `INFO`)
- `LOG_FORMAT`: `json` / `console` (default: `console`)

**Step 4: Implement unified error handling**

Create `app/common/error_codes.py` with constants:
- `VALIDATION_ERROR = 4001`
- `UNAUTHORIZED = 4002`
- `FORBIDDEN = 4003`
- `NOT_FOUND = 4041`
- `STATE_CONFLICT = 4091`
- `INTERNAL_ERROR = 5001`

Create `app/common/exceptions.py` with `FriendlyException`:
```python
class FriendlyException(Exception):
    def __init__(self, code: int, message: str, status_code: int = 400, data: dict | None = None): ...
```

Create `app/presentation/middlewares/exception_handler.py`:
- Catch `FriendlyException` → return `{"code": exc.code, "message": exc.message, "data": exc.data}` with `exc.status_code`
- Catch `RequestValidationError` → return code `4001`, status `422`
- Catch unhandled `Exception` → return code `5001`, status `500`, log full traceback

Create `app/presentation/api/v1/schemas/common.py` with generic `ResponseModel[T]`:
```python
class ResponseModel(BaseModel, Generic[T]):
    code: int = 0
    message: str = "ok"
    data: T | None = None
```

**Step 5: Implement structured logging**

Create `app/infrastructure/logging/setup.py`:
- Configure `structlog` with JSON processor chain for production, console renderer for dev
- Called from `create_app()` lifespan

**Step 6: Create the FastAPI app**

Create `app/main.py` with `create_app()` factory:
- Register `ExceptionHandlerMiddleware`
- Register health router at `/healthz`
- Initialize logging in lifespan
- Mount v1 routers under `/v1` prefix

Create `app/presentation/api/v1/routers/health.py`:
```python
router = APIRouter()

@router.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}
```

**Step 7: Add pytest fixtures with testcontainers**

Create `tests/conftest.py`:
- Use `testcontainers.postgres.PostgresContainer` to spin up a real PostgreSQL instance
- Create `async_sessionmaker` bound to the container
- Provide `db_session` fixture that wraps each test in a transaction and rolls back
- Provide `async_client` fixture using `httpx.AsyncClient` with `ASGITransport`

**Step 8: Run test**

Run: `pytest tests/api/test_health.py -v`

Expected: PASS.

---

### Task 2: Database Foundation

**Files:**
- Create: `app/infrastructure/persistence/models/base.py`
- Create: `app/infrastructure/persistence/models/user.py`
- Create: `app/infrastructure/persistence/models/wardrobe.py`
- Create: `app/infrastructure/persistence/models/import_task.py`
- Create: `app/infrastructure/persistence/models/recommendation.py`
- Create: `app/infrastructure/persistence/session.py`
- Create: `app/common/enums.py`
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/0001_initial.py`
- Test: `tests/unit/test_db_models.py`

**Step 1: Write model persistence tests**

Cover:
- Creating a user.
- Creating user settings with one `departure_time`.
- Creating a wardrobe item with status `wearable`.
- Creating a daily recommendation with one outfit and outfit items.
- Verifying `unique(user_id, recommendation_date)` constraint on `daily_recommendations`.

**Step 2: Implement SQLAlchemy models**

Create `app/infrastructure/persistence/models/base.py`:
- `Base` from `DeclarativeBase`
- `TimestampMixin` with `created_at`, `updated_at` using `server_default`
- Use UUID primary keys consistently

Implement only V1 Slim fields:

- `users`: `id`, `created_at`
- `user_settings`: `id`, `user_id` (unique), `departure_time`, `created_at`, `updated_at`
- `wardrobe_items`: `id`, `user_id`, `title`, `category`, `color_main`, `status`, `cover_image_url`, `original_image_url`, `deleted_at`, timestamps
- `wardrobe_item_images`: `id`, `item_id`, `image_type`, `image_url`, `created_at`
- `import_tasks`: `id`, `user_id`, `input_type`, `source_type`, `task_status`, `input_payload` (JSONB), `candidate_payload` (JSONB), `error_code`, `error_message`, timestamps
- `daily_recommendations`: `id`, `user_id`, `recommendation_date`, `status`, `generation_source`, `score_breakdown` (JSONB, nullable — for debugging engine sub-scores), timestamps. Unique constraint on `(user_id, recommendation_date)`.
- `outfits`: `id`, `daily_recommendation_id`, `user_id`, `outfit_type`, `title`, `reason_text`, `score_total`, `rank_order`, `created_at`
- `outfit_items`: `id`, `outfit_id`, `item_id`, `role`, `display_order`, `created_at`
- `outfit_feedback`: `id`, `user_id`, `outfit_id`, `feedback_type`, `created_at`
- `wear_history`: `id`, `user_id`, `outfit_id`, `selected_at`, `worn_date`, `created_at`

**Step 3: Implement session management**

Create `app/infrastructure/persistence/session.py`:
- `create_async_engine` from settings
- `async_sessionmaker` with `expire_on_commit=False`
- `get_db_session` async generator for FastAPI `Depends` — yields session, commits on success, rollbacks on exception

**Step 4: Enforce Slim enums in code**

Create `app/common/enums.py` with Python StrEnum:
- `ItemStatus`: `wearable`, `unavailable`
- `ImportStatus`: `waiting_confirm`, `completed`, `failed`
- `OutfitType`: `safe`, `fast`, `stylish`
- `FeedbackType`: `like`, `dislike`
- `RecommendationStatus`: `ready`, `insufficient_items`, `failed`
- `ImageType`: `original`, `standard`
- `Category`: `top`, `bottom`, `outerwear`, `dress`, `shoes`

**Step 5: Set up Alembic**

Configure `alembic.ini` and `alembic/env.py` for async PostgreSQL. Create initial migration.

**Step 6: Run tests**

Run: `pytest tests/unit/test_db_models.py -v`

Expected: PASS.

---

### Task 3: Temporary User Dependency

**Files:**
- Create: `app/presentation/api/v1/deps.py`
- Create: `app/application/users/__init__.py`
- Create: `app/application/ports/repositories.py` (user repo protocol)
- Create: `app/infrastructure/persistence/repositories/user_repo.py`
- Test: `tests/api/test_user_dependency.py`

**Step 1: Write dependency tests**

Cover:
- Missing `X-User-Id` header returns 401 Unauthorized.
- Valid `X-User-Id` header resolves or creates that user.
- In `prod` environment, `X-User-Id` mechanism is disabled (returns 401 for all requests).

**Step 2: Implement dependency**

Create `get_current_user()` in `deps.py`:
- Read `X-User-Id` from request headers
- **Guard**: if `settings.APP_ENV == "prod"`, always raise `FriendlyException(UNAUTHORIZED, "Authentication required", status_code=401)`
- If header is missing, raise `FriendlyException(UNAUTHORIZED, "X-User-Id header required", status_code=401)`
- If header is present, look up or create user via `UserRepository`

Keep the implementation isolated behind the `Depends(get_current_user)` boundary so real auth can replace it later by swapping this single dependency.

**Step 3: Run tests**

Run: `pytest tests/api/test_user_dependency.py -v`

Expected: PASS.

---

### Task 4: Settings API

**Files:**
- Create: `app/presentation/api/v1/routers/settings.py`
- Create: `app/presentation/api/v1/schemas/settings.py`
- Create: `app/application/users/settings.py`
- Create: `app/infrastructure/persistence/repositories/user_repo.py` (extend)
- Modify: `app/main.py`
- Test: `tests/api/test_settings.py`

**Step 1: Write API tests**

Cover:
- `GET /v1/user/settings` returns `departure_time` (wrapped in `ResponseModel`).
- `PATCH /v1/user/settings` updates only `departure_time`.
- Invalid time format returns 422 with error code `4001`.
- Response uses `ResponseModel` wrapper format.

**Step 2: Implement schemas and route**

Expose:
- `GET /v1/user/settings`
- `PATCH /v1/user/settings`

Route handlers call use case in `app/application/users/settings.py`. Do not implement `push_enabled` in V1 Slim.

All responses use `ResponseModel` wrapper.

**Step 3: Run tests**

Run: `pytest tests/api/test_settings.py -v`

Expected: PASS.

---

### Task 5: Upload Presign Adapter

**Files:**
- Create: `app/presentation/api/v1/routers/uploads.py`
- Create: `app/presentation/api/v1/schemas/uploads.py`
- Create: `app/application/ports/storage.py`
- Create: `app/infrastructure/storage/local_storage.py`
- Create: `app/bootstrap/container.py` (wire storage port)
- Test: `tests/api/test_uploads.py`

**Step 1: Write tests**

Cover:
- `POST /v1/uploads/presign` accepts image content types.
- Non-image content type is rejected with 422.
- Response includes `upload_url`, `file_url`, and `object_key` in `ResponseModel` wrapper.
- Presign enforces `max_content_length` of 10MB.

**Step 2: Implement storage port and local adapter**

Create `app/application/ports/storage.py`:
```python
class ObjectStoragePort(Protocol):
    async def create_presigned_upload(self, content_type: str, max_bytes: int) -> PresignResult: ...
```

Create `app/infrastructure/storage/local_storage.py`:
- Return deterministic local-style URLs in dev
- Interface shaped so S3/MinIO can replace the implementation

**Step 3: Wire in bootstrap**

Create `app/bootstrap/container.py` with a factory function that returns the correct storage adapter based on `settings.STORAGE_PROVIDER`.

**Step 4: Run tests**

Run: `pytest tests/api/test_uploads.py -v`

Expected: PASS.

---

### Task 6: Image Import Task API

**Files:**
- Create: `app/presentation/api/v1/routers/import_tasks.py`
- Create: `app/presentation/api/v1/schemas/import_tasks.py`
- Create: `app/application/ports/vision.py`
- Create: `app/infrastructure/vision/fake_vision.py`
- Create: `app/application/import_tasks/create_import.py`
- Create: `app/infrastructure/persistence/repositories/import_task_repo.py`
- Test: `tests/api/test_import_tasks.py`

**Step 1: Write tests**

Cover:
- `POST /v1/import-tasks` only accepts `input_type=image`.
- Created task immediately reaches `waiting_confirm` with `category` and `color_main`.
- `GET /v1/import-tasks/{id}` returns candidate item in `ResponseModel` wrapper.
- Failed vision response stores `failed` task status.
- Non-existent task returns 404 with code `4041`.
- Accessing another user's task returns 404 (ownership enforcement).

**Step 2: Implement vision port and fake adapter**

Create `app/application/ports/vision.py`:
```python
class VisionPort(Protocol):
    async def analyze_image(self, image_url: str) -> VisionResult: ...

@dataclass
class VisionResult:
    title: str
    category: str
    color_main: str
    confidence_score: float
```

Create `app/infrastructure/vision/fake_vision.py` with deterministic test responses.

**Step 3: Implement use case and routes**

Create `app/application/import_tasks/create_import.py` — orchestrates vision call, persists task.

Expose:
- `POST /v1/import-tasks`
- `GET /v1/import-tasks/{task_id}`

Do not implement retry, callback, link import, share import, or screenshot import.

**Step 4: Run tests**

Run: `pytest tests/api/test_import_tasks.py -v`

Expected: PASS.

---

### Task 7: Confirm Import Into Wardrobe

**Files:**
- Modify: `app/presentation/api/v1/routers/import_tasks.py`
- Create: `app/application/import_tasks/confirm_import.py`
- Create: `app/infrastructure/persistence/repositories/wardrobe_repo.py`
- Create: `app/presentation/api/v1/schemas/wardrobe.py`
- Test: `tests/api/test_import_confirm.py`

**Step 1: Write tests**

Cover:
- Confirming a `waiting_confirm` task creates a wardrobe item.
- Confirming a `completed` task returns 409 with error code `4091`.
- User can edit `title`, `category`, and `color_main` during confirmation.
- Response includes the new `item_id` in `ResponseModel` wrapper.
- `cover_image_url` defaults to `original_image_url` from the import task.

**Step 2: Implement confirmation use case**

Create `app/application/import_tasks/confirm_import.py`:
- Validates task status is `waiting_confirm`
- Creates `wardrobe_items` row with `cover_image_url = original_image_url`
- Creates `wardrobe_item_images` with `original` and `standard` image rows (both point to same URL in V1 Slim)
- Updates task status to `completed`
- Calls `schedule_today_recompute(user_id)` hook

Expose:
- `POST /v1/import-tasks/{task_id}/confirm`

**Step 3: Run tests**

Run: `pytest tests/api/test_import_confirm.py -v`

Expected: PASS.

---

### Task 8: Wardrobe CRUD API

**Files:**
- Create: `app/presentation/api/v1/routers/wardrobe.py`
- Create: `app/application/wardrobe/list_items.py`
- Create: `app/application/wardrobe/update_item.py`
- Create: `app/application/wardrobe/delete_item.py`
- Modify: `app/main.py`
- Test: `tests/api/test_wardrobe.py`

**Step 1: Write tests**

Cover:
- `GET /v1/wardrobe-items` returns non-deleted user items with pagination (`limit`/`offset`).
- `GET /v1/wardrobe-items` returns `total` count for client-side pagination.
- `GET /v1/wardrobe-items/{id}` enforces ownership (other user's item → 404).
- `PATCH /v1/wardrobe-items/{id}` updates title/category/color/status.
- Only `wearable` and `unavailable` are accepted for status.
- `DELETE /v1/wardrobe-items/{id}` soft deletes (sets `deleted_at`).
- All responses use `ResponseModel` wrapper.

**Step 2: Implement routes**

Expose:
- `GET /v1/wardrobe-items` with query params: `category` (optional), `limit` (default 20, max 100), `offset` (default 0)
- `GET /v1/wardrobe-items/{item_id}`
- `PATCH /v1/wardrobe-items/{item_id}`
- `DELETE /v1/wardrobe-items/{item_id}`

Route handlers delegate to use cases. Use cases call repository through port interfaces.

**Step 3: Trigger recommendation recompute hook**

After confirm (Task 7), patch status, or delete, call a service hook `schedule_today_recompute(user_id)`. In tests this can be a no-op spy.

**Step 4: Run tests**

Run: `pytest tests/api/test_wardrobe.py -v`

Expected: PASS.

---

### Task 9: Recommendation Engine

**Files:**
- Create: `app/application/recommendations/engine.py`
- Create: `app/application/recommendations/colors.py`
- Test: `tests/unit/test_recommendation_engine.py`

**Step 1: Write engine tests**

Cover:
- Fewer than 3 wearable items returns `insufficient_items`.
- 3 wearable items can produce at least 1 outfit when categories allow.
- Top + bottom combinations are generated.
- Three final outfits do not repeat the main item.
- Recent worn main item is downgraded.
- Reasons are template-generated for `safe`, `fast`, and `stylish`.
- Engine returns sub-scores (color_score, freshness_score) alongside total for debugging.

Note: Engine is pure logic — no database, no async, no framework imports. Takes plain dataclasses/dicts as input. This makes it trivially unit-testable.

**Step 2: Implement minimal category logic**

Use categories:
- `top`
- `bottom`
- `outerwear`
- `dress`
- `shoes`

Main item is `top` or `dress`.

**Step 3: Implement color conflict table**

Keep a small hardcoded table. Do not implement color science.

**Step 4: Implement scoring**

Use only:
- color coordination
- freshness from `wear_history`
- type-specific boost for safe/fast/stylish

Return `score_breakdown` dict alongside `score_total` for each outfit.

**Step 5: Run tests**

Run: `pytest tests/unit/test_recommendation_engine.py -v`

Expected: PASS.

---

### Task 10: Recommendation Persistence

**Files:**
- Create: `app/application/recommendations/generator.py`
- Create: `app/infrastructure/persistence/repositories/recommendation_repo.py`
- Test: `tests/services/test_recommendation_generator.py`

**Step 1: Write generator tests**

Cover:
- Generating for a date creates one `daily_recommendations` row.
- Regenerating same user/date replaces or updates prior outfits predictably (upsert behavior).
- Insufficient items persists `insufficient_items` status.
- Ready recommendations persist outfits, outfit_items, and `score_breakdown` JSONB.
- Concurrent calls to `generate_for_user` with same user/date do not raise (handles unique constraint gracefully).

**Step 2: Implement generator**

Implement `generate_for_user(user_id, target_date, generation_source)`.

Use a transaction. Handle `unique(user_id, recommendation_date)` via upsert pattern — on conflict, delete old outfits and regenerate. Log generation events via structlog.

**Step 3: Run tests**

Run: `pytest tests/services/test_recommendation_generator.py -v`

Expected: PASS.

---

### Task 11: Today Recommendation API

**Files:**
- Create: `app/presentation/api/v1/routers/recommendations.py`
- Create: `app/presentation/api/v1/schemas/recommendations.py`
- Create: `app/application/recommendations/get_today.py`
- Modify: `app/main.py`
- Test: `tests/api/test_recommendations_today.py`

**Step 1: Write API tests**

Cover:
- Existing ready recommendation returns immediately in `ResponseModel` wrapper.
- Existing `insufficient_items` result returns guidance text, not empty loading.
- Missing today recommendation returns yesterday or fallback if available and schedules background recompute.
- No cached result at all returns `insufficient_items` guidance, not `generating`.
- Response never contains `status: "generating"` — this is a hard contract.

**Step 2: Implement route**

Expose:
- `GET /v1/recommendations/today`

Do not block on expensive generation in the request path. If no recommendation exists, schedule background generation and return fallback.

**Step 3: Run tests**

Run: `pytest tests/api/test_recommendations_today.py -v`

Expected: PASS.

---

### Task 12: Outfit Detail, Wear, and Feedback APIs

**Files:**
- Modify: `app/presentation/api/v1/routers/recommendations.py`
- Create: `app/application/outfits/wear.py`
- Create: `app/application/outfits/feedback.py`
- Test: `tests/api/test_outfit_actions.py`

**Step 1: Write tests**

Cover:
- `GET /v1/recommendations/outfits/{id}` returns outfit items in `ResponseModel` wrapper.
- `POST /v1/recommendations/outfits/{id}/wear` creates one wear history row.
- Wearing the same outfit twice is idempotent for the same date.
- `POST /v1/recommendations/outfits/{id}/feedback` accepts only `like` and `dislike`.
- Invalid feedback type returns 422.
- Accessing another user's outfit returns 404.

**Step 2: Implement routes**

Expose:
- `GET /v1/recommendations/outfits/{outfit_id}`
- `POST /v1/recommendations/outfits/{outfit_id}/wear`
- `POST /v1/recommendations/outfits/{outfit_id}/feedback`

Do not implement reason tags.

**Step 3: Run tests**

Run: `pytest tests/api/test_outfit_actions.py -v`

Expected: PASS.

---

### Task 13: Background Recompute and Scheduler

**Files:**
- Create: `app/jobs/scheduler.py`
- Create: `app/jobs/recommendation_jobs.py`
- Test: `tests/services/test_recommendation_background.py`

**Step 1: Write tests**

Cover:
- Wardrobe mutation schedules a recompute for today.
- 03:00 fallback selects users without `departure_time`.
- `departure_time - 90 minutes` generation selects due users.
- Debounce: if `daily_recommendations.updated_at` is within 30 seconds, recompute is skipped.
- Multiple rapid wardrobe mutations do not trigger multiple concurrent recomputes.

**Step 2: Implement background service**

Use `FastAPI BackgroundTasks` in route handlers for mutation-triggered recompute.

`schedule_today_recompute(user_id)` implementation:
- Check `daily_recommendations.updated_at` for debounce (skip if < 30 seconds ago)
- If not debounced, call `generator.generate_for_user`
- Log via structlog

APScheduler cron jobs in `app/jobs/recommendation_jobs.py`:
- `cron_departure_pre_generate()`: runs every 5 minutes, selects users whose `departure_time - 90 min` falls in the current window, generates for each
- `cron_nightly_fallback()`: runs at 03:00, generates for users without `departure_time` or with `departure_time < 05:30`

**Step 3: Wire startup/shutdown**

Create `app/jobs/scheduler.py`:
- Initialize APScheduler with `AsyncIOScheduler`
- **Multi-worker safety**: Document that scheduler must run in a dedicated process or single-worker deployment. Add a startup log warning if `ENABLE_SCHEDULER=true` to remind operators.
- Start scheduler only when `settings.ENABLE_SCHEDULER == True`
- Shut down scheduler in lifespan `shutdown`

Modify `app/main.py` lifespan to start/stop scheduler.

**Step 4: Run tests**

Run: `pytest tests/services/test_recommendation_background.py -v`

Expected: PASS.

---

### Task 14: API Contract Smoke Tests

**Files:**
- Create: `tests/api/test_v1_slim_flow.py`

**Step 1: Write full flow test**

Flow:
1. Patch departure time.
2. Presign upload.
3. Create image import task.
4. Confirm task into wardrobe.
5. Repeat until at least top and bottom exist.
6. Generate recommendation directly via service.
7. Fetch today recommendation.
8. Mark one outfit as worn.
9. Submit like/dislike feedback.

**Step 2: Write edge case flow tests**

Additional cases:
- **Insufficient items flow**: With < 3 items, `GET /v1/recommendations/today` returns `insufficient_items` with guidance text, never an empty/loading state.
- **Wardrobe mutation flow**: After soft-deleting a wardrobe item, regenerated recommendation no longer includes that item.
- **Soft delete exclusion**: Deleted items do not appear in `GET /v1/wardrobe-items` list.

**Step 3: Run full flow**

Run: `pytest tests/api/test_v1_slim_flow.py -v`

Expected: PASS.

---

### Task 15: Migration Verification

**Step 1: Verify migration up/down cycle**

Run: `alembic upgrade head && alembic downgrade base && alembic upgrade head`

Expected: No errors. This validates that migrations are reversible and idempotent.

---

### Task 16: Documentation Sync

**Files:**
- Create: `docs/api-v1-slim.md`
- Create: `docs/dev-setup.md`
- Modify: `docs/README.md`

**Step 1: Document implemented API**

List only implemented Slim endpoints and show request/response examples. Include `ResponseModel` wrapper format.

**Step 2: Document local setup**

Include:
- `make install`
- Environment variables (reference `.env.example`)
- `make migrate-apply`
- `make test`
- `make dev`
- Note: scheduler requires `ENABLE_SCHEDULER=true` and must run in single-worker mode

**Step 3: Document deployment notes**

Include:
- Scheduler must run as a separate process or single-worker instance to avoid duplicate job execution across workers.
- `APP_ENV=prod` disables `X-User-Id` dev auth — real auth must be implemented before production.

**Step 4: Update docs index**

Add `docs/plans/2026-05-01-v1-slim-backend-mvp.md`, `docs/api-v1-slim.md`, and `docs/dev-setup.md` to `docs/README.md`.

**Step 5: Verify docs**

Run: `pytest -q`

Expected: PASS.

---

## Verification Checklist

- `pytest -q` passes.
- `ruff check .` passes.
- `mypy app` passes.
- `alembic upgrade head && alembic downgrade base && alembic upgrade head` succeeds.
- `/healthz` returns `{"status": "ok"}`.
- `GET /v1/recommendations/today` never returns a blank/loading-only state.
- With 3 useful wardrobe items, service returns at least one outfit.
- Three outfit cards do not repeat the main item when enough candidates exist.
- All API responses use `ResponseModel` wrapper format.
- `X-User-Id` auth is disabled when `APP_ENV=prod`.
- Missing `X-User-Id` returns 401, not auto-created user.
- No link/share/import retry/push/weather/embedding/LLM behavior accidentally enters V1 Slim.
- Structured logs appear for: vision API calls, recommendation generation, scheduler triggers, error responses.

## Implementation Notes

- Keep route handlers thin; put behavior in use cases under `application/`.
- Use cases depend on Protocol interfaces, not concrete implementations.
- Prefer deterministic rule code over model calls in tests.
- Do not add a generic abstraction until a second implementation exists.
- Keep all V1 Slim enum choices narrow. Extra fields can exist in the database only if they do not create UI/API obligations.
- Real vision and S3 providers should sit behind small interfaces and remain disabled in tests.
- All tests run against real PostgreSQL via testcontainers — no SQLite.
- `score_breakdown` JSONB on `daily_recommendations` is for debugging only; do not expose in API responses.
- Presign enforces `max_content_length` of 10MB to prevent storage abuse.
- Recompute debounce: skip if `daily_recommendations.updated_at` < 30 seconds ago.
- APScheduler runs in a single process only; document this constraint for production deployment.
