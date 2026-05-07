# API Changelog

Records OpenAPI / behavior changes per merged change-set. Newest first.

Tag legend per entry:
- **add-only** — purely additive (new optional field, new endpoint, new optional query param). Old clients keep working.
- **breaking** — removed / renamed / type-tightened in a way that may reject previously-accepted payloads.
- **behavior** — same wire shape, semantics changed.

---

## 2026-05-07 — Phase B (Explainability Runtime, behind feature flags)

All wire changes are **add-only** and inert until the corresponding flag flips on. Default off in every environment.

### Schema field additions

- `OutfitResponse`:
  - `scores: { aesthetic, feasibility, fit, total, prep_minutes, rules_version } | null` — present only when `ENABLE_OUTFIT_SCORES=true`.
  - `obstacles: [{ kind: laundry|lost|seasonal|stale, item_id, severity: blocking|warning|info, title: I18nMessage }] | null` — present only when `ENABLE_OBSTACLES=true`.
  - `prep_steps: I18nMessage[] | null`, `prep_notes: I18nMessage[] | null` — present only when `ENABLE_PREP_STEPS=true`.
- New shared shape `I18nMessage = { key, params, fallback_text }`. Server always populates `fallback_text` (zh-CN). Clients prefer `key + params` against their own locale bundle.

### Database migration

- `0021_add_outfit_explainability` — `outfits` adds `score_breakdown_json JSONB NULL`, `score_rules_version VARCHAR(16) NULL`, `obstacles_json JSONB NULL`, `prep_json JSONB NULL`. All NULLable, no defaults → metadata-only ALTER. Legacy outfits keep working with everything `NULL`; the dynamic scoring layer falls back to `aesthetic=0.5, prep_base=2` when `score_breakdown_json` is absent.

### Feature flags (settings)

- `ENABLE_OUTFIT_SCORES: bool = False`
- `ENABLE_OBSTACLES: bool = False`
- `ENABLE_PREP_STEPS: bool = False`

When all three are off, GET `/today` and GET `/outfits/{id}` skip the WardrobeItem batch fetch entirely — zero overhead vs. Phase A.

### Configuration / files added

- `app/config/scoring_rules.yaml` v1.0 — aesthetic component weights, prep_minutes formula, fit-score map. Versioned; persisted `score_rules_version` per outfit.
- `app/config/freshness_rules.yaml` v1.0 — per-category rest_days and `stale_threshold_days=90`.
- `app/common/i18n.py` — `I18nMessage` model + `make_message()` helper that emits `i18n.rendered` structlog event.
- `app/application/outfits/scoring/{rules,static,dynamic}.py` — yaml loader with lru_cache, persisted-at-creation static breakdown, GET-time dynamic recomputation.
- `app/application/outfits/obstacles.py` — lightweight detection (dirty/lost/seasonal/stale). Weather + replacement-candidate ranking deferred to Phase D.
- `app/application/outfits/prep.py` — location grouping + delicate-material care notes.
- `tests/unit/test_outfit_explainability.py` — 20 pure-function tests across scoring, obstacles, prep.

### structlog events (new)

`scoring.computed { layer, rule_version, ... }`, `obstacle.detected { rule_version, count, kinds }`, `prep.generated { steps_count, notes_count, locations }`, `i18n.rendered { key, rule_version, param_keys }`.

### Known follow-ups (still scoped to later phases)

- Static breakdown not yet computed inside the daily-recommendation generation pipeline — `score_breakdown_json` will stay NULL for new outfits until that's wired (separate task).
- `obstacles.kind=weather_mismatch` and replacement-candidate ranking — Phase D.
- Rule-versioned A/B switching framework — Phase D/E.

---

## 2026-05-06 — Phase A (Contract + Consistency)

Driven by `docs/backend-gaps-plan.md` Phase A. Eleven sub-tasks landed in one wave.

### Endpoints added

- **add-only** `GET /v1/wardrobe-items/{item_id}/wear-history?days=60` — pre-aggregated daily wear counts + raw entries for ItemDetail heatmap. `days` is `int 1..365`, default 60. Returns `ItemWearHistoryResponse` ({ `item_id`, `days`, `daily_counts: {YYYY-MM-DD: int}`, `entries: WearHistoryEntry[]` }). 404 if item not owned by current user.
- **add-only** `POST /v1/recommendations/outfits/{outfit_id}/feedback/bundle` — single-transaction (savepoint) all-or-nothing batch: one outfit-level feedback + N item-level fit-feedbacks. Returns `{ written: int }`. Partial failure rolls back the whole bundle.
- **add-only** `GET /v1/recommendations/feedback-tags?scene=` — config-driven reason-tag catalog from `app/config/feedback_tags.yaml`. `scene` optional (32-char free-form). Unknown scene returns default tags (200, not 404). Tags carry `key`, `label_zh_cn`, `applies_to: ["overall"|"item"][]`.

### Schema field additions (add-only)

- `WardrobeItemResponse`:
  - `last_worn: date | null` — derived in DB via wear path; surfaced for ItemDetail "最近穿着" line.
  - `purchase_date: date | null` — populated via PATCH.
- `UpdateWardrobeItemRequest`:
  - `purchase_date: date | null`.
- `RecommendationItemResponse`:
  - `display_role: "top" | "outer" | "bottom" | "shoes" | "accessory"` (default `accessory`) — drives Today / OutfitDetail hero layout. Mapped from `category` via `app/common/category_to_role.py`.

### Schema field tightening (breaking — but limited blast radius)

- `WardrobeItemResponse.status`: `string` → `WardrobeItemStatus` enum (`wearable | unavailable | dirty | lost | donated`). Old clients sending arbitrary status strings via PATCH will now get 422. Old clients READING items receive a `status` value still in the same 5 strings (no change for read path).
- `UpdateWardrobeItemRequest.status`: same tightening as above. Transition matrix from `docs/wardrobe-status-machine.md` is **not** enforced at the API boundary yet — only enum validation.

### Schema behavior changes (additive but worth noting)

- `FeedbackRequest.feedback_type` pattern: `^(like|dislike)$` → `^(love|like|neutral|dislike)$`. Old clients still accepted. `build_style_profile` updated to 4 explicit branches with weights `love=+1.5, like=+1.0, neutral=0.0, dislike=-1.0`. Unknown values raise `ValueError`.
- `FeedbackType` enum extended with `LOVE` and `NEUTRAL`.

### Database migrations

- `0018_add_daily_recommendation_versioning` — `daily_recommendations` adds `revision SMALLINT NOT NULL DEFAULT 1` and `is_current BOOLEAN NOT NULL DEFAULT TRUE`. Old uniqueness `(user_id, recommendation_date)` replaced by `(user_id, recommendation_date, revision)` plus partial unique `WHERE is_current`. Refresh use-case (Phase D) will write a new revision instead of overwriting.
- `0019_add_last_worn_purchase_date` — `wardrobe_items` adds `last_worn_at DATE NULL` and `purchase_date DATE NULL`. Backfill `last_worn_at` from `MAX(wear_history.worn_date)` joined via `outfit_items`, in batches of 5000. Concurrent index `ix_wardrobe_items_user_last_worn (user_id, last_worn_at DESC NULLS LAST)`.
- `0020_wear_history_idempotency` — `wear_history` adds unique constraint `(user_id, outfit_id, worn_date)` via `CREATE UNIQUE INDEX CONCURRENTLY`. Defensive dedup (ROW_NUMBER over `created_at`, tiebreak by `id::text` — PG has no `MIN(uuid)` aggregate) drops earlier duplicates. Wear path uses `INSERT ... ON CONFLICT DO NOTHING`; conflict counts as no-op success and does **not** double-increment `wear_count` or update `last_worn_at`.
- `alembic/env.py` — defensively widens `alembic_version.version_num` to `VARCHAR(128)` at every connect (default 32 cannot fit our descriptive revision IDs).

### Other behavior changes

- Wear path (`POST /v1/recommendations/outfits/{id}/wear`):
  - Idempotent on `(user, outfit, date)` — second POST is a 200 no-op.
  - `last_worn_at` updates only forward: `GREATEST(COALESCE(existing, :worn), :worn)` ensures wearing an older date does not regress the value.
  - `wear_count` increment runs ONLY when the conflict insert produced a new row.
- Two new HTTP middlewares (registered outside `InMemoryRateLimitMiddleware`):
  - `CorrelationIdMiddleware` — reads/generates `X-Correlation-Id`, binds to structlog contextvars, echoes in response.
  - `TimingMiddleware` — `X-Process-Time-Ms` integer header + structlog `request.completed`.

### Configuration / files added

- `app/common/enums.py` — `WardrobeItemStatus` (5 values).
- `app/common/category_to_role.py` — category → display_role mapping with `accessory` fallback + `category.unknown_role` warning.
- `app/config/feedback_tags.yaml` — reason-tag catalog (default + commute/formal/casual/date/travel scenes).
- `app/application/recommendations/feedback_tags.py` — yaml loader with `lru_cache(1)` + `reload_for_tests()`.
- `docs/wardrobe-status-machine.md` — transition matrix; explicitly rejects auto `wear → dirty`.
- `docs/perf-baseline-2026-05.md` — 11-dimension perf target table (numbers TBD).

### Known follow-ups (scoped to later phases per `docs/backend-gaps-plan.md`)

- Status transition matrix enforcement at PATCH boundary — Phase A leaves enum validation only.
- Refresh endpoint that writes a new `revision` and demotes prior `is_current` — Phase D.
- Freshness derived from wear_history + per-category rules — Phase B together with obstacles.
- i18n key + params + fallback_text contract for `obstacles` / `prep` — Phase B.
- Cost guard / privacy purge / async job framework — Phase C–E.
