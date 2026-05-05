# V1 Slim Backend Architecture

Last updated: 2026-05-01

## Purpose

This document maps the implemented V1 Slim backend so future work can start from the code instead of reconstructing decisions from chat history.

## Runtime Shape

The app is a FastAPI service backed by PostgreSQL through async SQLAlchemy. Alembic owns schema changes. Tests run against PostgreSQL testcontainers rather than SQLite so query behavior and migrations stay close to production.

## Module Map

- `app/main.py`: application factory, middleware registration, router inclusion, and scheduler lifecycle.
- `app/config/settings.py`: environment-driven settings loaded from `.env`.
- `app/common`: shared enums, error codes, and `FriendlyException`.
- `app/presentation/api/v1`: HTTP routers, request/response schemas, and API dependencies.
- `app/presentation/middlewares`: exception translation into the unified API envelope.
- `app/application`: use cases and domain workflow logic.
- `app/application/ports`: storage, vision, and repository interfaces.
- `app/infrastructure/persistence`: SQLAlchemy session setup, ORM models, and repository implementations.
- `app/infrastructure/storage`: local fake storage adapter for V1 Slim upload presign behavior.
- `app/infrastructure/vision`: fake deterministic image parser used by import tasks.
- `app/jobs`: recommendation recompute job and optional APScheduler integration.

## Data Model

The initial migration creates tables for:

- users
- user settings
- wardrobe items
- import tasks
- recommendations
- outfits
- outfit items
- outfit feedback
- wear history

The current schema is intentionally narrow and supports the image-only V1 Slim flow.

## Request Flow

1. A router receives the request and validates it with Pydantic schemas.
2. API dependencies resolve the current user, database session, and repository-backed services.
3. Application use cases perform workflow logic and raise `FriendlyException` for expected user-facing failures.
4. Infrastructure repositories persist state through async SQLAlchemy.
5. The presentation layer returns a unified response envelope.

## Recommendation Flow

Recommendations are generated from confirmed wardrobe items and user settings. The generator builds a small candidate outfit set, persists the recommendation and outfits, and exposes the current day's result through the API.

Outfit feedback and wear actions are stored separately so later recommendation iterations can learn from explicit user responses and actual wear behavior.

## Current Boundaries

- Authentication is a development stub using `X-User-Id`; production mode rejects this shortcut.
- Vision extraction is deterministic fake parsing, not a real model integration.
- Storage is a local fake adapter, not object storage.
- Weather and calendar context are not integrated yet.
- The scheduler is optional and disabled by default through `ENABLE_SCHEDULER=false`.
