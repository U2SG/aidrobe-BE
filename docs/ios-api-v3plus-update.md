# iOS API Integration — V3+ Update (Phases A → E)

Companion to [`ios-api-alignment.md`](./ios-api-alignment.md). Covers everything
new since the original V3 contract: explainability runtime, recommendation
versioning + refresh, replacement candidates, OSS migration with STS, account
deletion (PIPL), cost guard, and DSAR export.

> **Read order**: skim §1 (what changed at a glance), then jump to the sections
> that touch the screens you're building. iOS-specific gotchas are flagged
> with **🔶 iOS** call-outs.

---

## 1. What changed at a glance

| Surface | Phase | Status | Backend ref |
|---|---|---|---|
| 4-tier outfit feedback (`love/like/neutral/dislike`) | A | shipped | [§2](#2-feedback--style-signal) |
| `POST /outfits/{id}/feedback/bundle` | A | shipped | [§2](#2-feedback--style-signal) |
| `GET /v1/wardrobe-items/{item_id}/wear-history` | A | shipped | [§3](#3-wear-history--insights) |
| `daily_recommendations.revision` versioning | A → D3 | shipped | [§4](#4-recommendation-versioning--refresh) |
| `OutfitResponse.scores / obstacles / prep_steps / prep_notes` | B | flag-gated | [§5](#5-explainability-scores--obstacles--prep) |
| i18n contract `{key, params, fallback_text}` | B | shipped | [§5.4](#54-i18n-contract) |
| `TodayRecommendationResponse.context` (weather + scene) | D1 | flag-gated | [§4.2](#42-today-context) |
| `POST /v1/recommendations/today/refresh` | D3 | flag-gated | [§4.3](#43-today-refresh) |
| `Obstacle.replacement_candidate_ids` + `weather_mismatch` | D2 | shipped (when obstacles flag on) | [§5.3](#53-replacement-candidates) |
| `image_url` becomes signed (private bucket) | E5 | shipped on Aliyun deploys | [§6](#6-storage--signed-urls) |
| `POST /v1/uploads/sts-token` | E5 | flag-gated | [§6.2](#62-sts-token) |
| `DELETE /v1/me` (soft delete + grace) | E1 | flag-gated | [§7](#7-account-lifecycle--pipl) |
| Apple-login auto-reactivation in grace | E1 | shipped | [§7.2](#72-reactivation) |
| `POST /v1/me/data-export` | E5 | shipped | [§7.3](#73-dsar-data-export) |
| `GET /v1/user/cost-summary` | E3 | shipped | [§8](#8-cost-summary) |

**Feature flags (server-side, default OFF unless noted)**:

| Flag | Controls | Default |
|---|---|---|
| `ENABLE_OUTFIT_SCORES` | OutfitResponse.scores | OFF |
| `ENABLE_OBSTACLES` | OutfitResponse.obstacles + replacement candidates + weather_mismatch | OFF |
| `ENABLE_PREP_STEPS` | OutfitResponse.prep_steps + prep_notes | OFF |
| `ENABLE_TODAY_CONTEXT` | TodayRecommendationResponse.context | OFF |
| `ENABLE_TODAY_REFRESH` | POST /today/refresh (returns 503 when off) | OFF |
| `ENABLE_USER_PURGE` | DELETE /me (returns 503 when off) | OFF |
| `ENABLE_OSS_STS` | POST /uploads/sts-token (returns 503 when off) | OFF |
| `ENABLE_COST_GUARD` | quota enforcement on cost-bearing jobs | OFF |

🔶 **iOS**: probe the flags via response shape rather than hard-coding. All new
fields are nullable + additive — when a flag is off the field is `null` or
absent, never an error. Build UI to render gracefully.

---

## 2. Feedback — 4-tier signal

`feedback_type` now accepts **four** values: `love | like | neutral | dislike`.
Old `like` / `dislike` callers continue to work.

### 2.1 `POST /v1/recommendations/outfits/{outfit_id}/feedback`

```json
{ "feedback_type": "love", "reason_tags": ["color_match", "weather_fit"] }
```

Reason tag dictionary at `GET /v1/recommendations/feedback-tags?scene=commute`:

```json
{
  "data": {
    "scene": "commute",
    "tags": [
      { "key": "color_match", "label_zh_cn": "颜色搭得好", "applies_to": ["overall", "item"] },
      ...
    ]
  }
}
```

### 2.2 `POST /v1/recommendations/outfits/{outfit_id}/feedback/bundle`

Single-transaction overall + per-item feedback. All-or-nothing — on validation
error (e.g. an item_id not in the outfit) the whole call rolls back and
returns **422** (`error_code: 4001`, message describes which item failed).

```json
{
  "overall": { "feedback_type": "love", "reason_tags": [] },
  "items": [
    { "item_id": "<uuid>", "feedback_type": "fits_well", "scene": "commute", "comfort_score": 4 },
    ...
  ]
}
```

Response: `{ "data": { "written": <int> } }` — total rows persisted.

🔶 **iOS**: on 422, do NOT show partial success. The server guarantees nothing
was written. Re-validate the bundle (commonly: the user picked an item from
yesterday's outfit; refresh the screen).

---

## 3. Wear history & insights

### 3.1 `GET /v1/wardrobe-items/{item_id}/wear-history?days=60`

Returns pre-aggregated daily counts for a heatmap. Capped at 365 days.

```json
{
  "data": {
    "item_id": "<uuid>",
    "days": 60,
    "daily_counts": [
      { "date": "2026-04-01", "count": 1 },
      { "date": "2026-04-02", "count": 0 },
      ...
    ]
  }
}
```

`wardrobe_items` now also carry `last_worn_at` and `purchase_date` (DATE,
nullable). These are denormalized — updated atomically on the wear path.

---

## 4. Recommendation versioning + refresh

### 4.1 Revisioning model

`daily_recommendations` rows now carry `revision` (default 1) and `is_current`
(default true). Each refresh archives the current row (`is_current=false`) and
inserts a new row with `revision=max+1, is_current=true`.

**Crucially**: `outfit_id` of an old revision **stays valid forever**. Wear
history and feedback rows reference outfit_ids; users can browse what they
actually wore even months after a refresh.

`GET /v1/recommendations/today` always returns the `is_current=true` row.
`GET /v1/recommendations/outfits/{outfit_id}` resolves any outfit_id, current
or archived.

### 4.2 Today context

When `ENABLE_TODAY_CONTEXT=true`, `/today` includes a `context` field:

```json
{
  "data": {
    "recommendation_date": "2026-05-08",
    "status": "ready",
    "outfits": [...],
    "context": {
      "weather": {
        "temperature_c": 18.5,
        "feels_like_c": 17.0,
        "rain_probability": 0.4,
        "condition": "cloudy"
      },
      "scene": "commute",
      "fetched_at": "2026-05-08T07:00:00+00:00"
    }
  }
}
```

When the flag is off, `context` is `null` (still in the response shape).
When the weather provider was down at generation time, `context` may be
`null` even with the flag on — fail-soft contract.

🔶 **iOS**: don't rely on `context.weather` being present. Render a
"weather-aware" badge only when `context.weather is not None`.

### 4.3 Today refresh

`POST /v1/recommendations/today/refresh` (gated by `ENABLE_TODAY_REFRESH`).

**Status codes**:
- `200` — new revision generated, response is the standard `TodayRecommendationResponse`
- `429` — daily refresh limit exhausted (`TODAY_REFRESH_DAILY_LIMIT`, default 3)
- `503` — flag is off

Idempotency / concurrency:
- Concurrent refreshes for the same user-day **serialize** via Postgres
  advisory lock — safe to fire from a tap-debounced button.
- The previous revision's outfits remain reachable by `outfit_id`, so a
  user can still see "the outfit I wore" after a refresh.

🔶 **iOS**: invalidate any local /today cache after a successful refresh.
The server invalidates its Redis cache; client-side caches are your
responsibility.

---

## 5. Explainability — scores / obstacles / prep

When the corresponding flags are on, `OutfitResponse` carries:

```json
{
  "outfit_id": "<uuid>",
  "type": "safe",
  "title": "...",
  "reason": "...",
  "items": [...],
  "scores": { ... },                    // ENABLE_OUTFIT_SCORES
  "obstacles": [ ... ],                 // ENABLE_OBSTACLES
  "prep_steps": [ I18nMessage, ... ],   // ENABLE_PREP_STEPS
  "prep_notes": [ I18nMessage, ... ]    // ENABLE_PREP_STEPS
}
```

When a flag is off the field is **`null`** (Pydantic-serialized) — render
empty rather than asserting presence.

### 5.1 Scores

```json
{
  "aesthetic": 0.91,
  "feasibility": 0.85,
  "fit": 0.92,
  "total": 0.89,
  "prep_minutes": 6,
  "rules_version": "1.0"
}
```

`prep_minutes` is the predicted time to actually put the outfit together
(adjusted for current item state — e.g. higher if something needs ironing).

### 5.2 Obstacles

```json
{
  "kind": "laundry" | "lost" | "seasonal" | "stale" | "weather_mismatch",
  "item_id": "<uuid>",
  "severity": "blocking" | "warning" | "info",
  "title": { "key": "...", "params": {...}, "fallback_text": "..." },
  "replacement_candidate_ids": ["<uuid>", "<uuid>", "<uuid>"]
}
```

`item_id` is empty string `""` for `weather_mismatch` (applies to the whole
outfit, not a single item).

### 5.3 Replacement candidates

`replacement_candidate_ids` is up to 3 wardrobe-item ids the client can
suggest as replacements. Cascade order on the server:

1. Embedding similarity (when CLIP enabled)
2. Same category + same color
3. Same category only
4. `[]` if nothing wearable matches

All candidates are wearable-status-filtered at request time.

🔶 **iOS**: render the FIRST id that's still wearable. Server already filters,
but item state can change between request and tap — fall back to the next
one if the user's selection 404s.

`stale` and `weather_mismatch` obstacles intentionally have **no**
replacement_candidate_ids — those obstacles target the look as a whole or
the freshness of an otherwise-fine item.

### 5.4 i18n contract

Every user-facing string from the explainability runtime ships as:

```json
{
  "key": "obstacle.laundry",
  "params": { "item_title": "黑色西裤" },
  "fallback_text": "黑色西裤在脏衣篮"
}
```

Render order:
1. If you have a translation for `key`, interpolate `params` into it
2. Otherwise show `fallback_text` verbatim (already has reasonable zh-CN)

`fallback_text` is **always present** — never null — so a brand-new client
that hasn't pulled translations still renders something sensible.

---

## 6. Storage — signed URLs

Phase E5 migrates the production deploy to Aliyun OSS private buckets. Two
consequences for iOS:

### 6.1 `image_url` is now short-lived

`OutfitVisualAssetResponse.image_url` and the per-image entries in
`/v1/wardrobe-items/{item_id}/images` now return **signed URLs** when the
backend storage is private. Default TTL is **30 minutes**.

🔶 **iOS critical**:
- **DO NOT** persist these URLs to local storage / disk cache as long-lived references
- **DO** cache the in-memory `Image` for the session, not the URL
- **DO** re-fetch via the parent endpoint when you need the same image after
  the TTL elapses
- For `original` images uploaded via the import flow (`object_key=null`,
  `image_url=external`), the URL is the raw third-party URL and stays valid
  per that host's policies

### 6.2 STS token

`POST /v1/uploads/sts-token` (gated by `ENABLE_OSS_STS`) issues
narrow-scoped, short-lived OSS credentials so the client uploads originals
direct-to-OSS without the bytes traversing the backend.

Request:
```json
{ "bucket_alias": "items" | "ootd" | "private" | "derived",
  "action": "upload" | "read" }
```

Response:
```json
{
  "data": {
    "access_key_id": "STS.NTwx...",
    "access_key_secret": "...",
    "security_token": "...",
    "expiration": "2026-05-08T07:15:00Z",
    "bucket": "aidrobe-items-prod",
    "region": "cn-hangzhou",
    "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
    "object_key_prefix": "users/<your-uuid>/upload/"
  }
}
```

The token can ONLY:
- Hit `users/<your-uuid>/upload/*` of the named bucket (no other prefix; no other user)
- For the requested verb (upload tokens can't read; read tokens can't delete)
- For ~15 minutes (configurable, currently the default)

iOS upload pattern (using AlibabaCloud iOS SDK or pure-S3-compat client):
1. Request STS token before upload
2. Construct the object key as `<object_key_prefix><filename>` (the prefix
   already encodes user_id + action)
3. PUT directly to OSS
4. Return the object key to the backend (e.g. as part of an item creation
   request) — the backend signs render URLs at view time

When `ENABLE_OSS_STS=false`, fall back to `POST /v1/uploads/presign` (server
signs a single PUT URL per call).

---

## 7. Account lifecycle — PIPL

### 7.1 Soft delete

`DELETE /v1/me` (gated by `ENABLE_USER_PURGE`).

Response:
```json
{
  "data": {
    "deactivated_at": "2026-05-08T07:00:00Z",
    "scheduled_purge_at": "2026-06-07T07:00:00Z",
    "grace_days": 30
  }
}
```

After this call:
- All active sessions are revoked. The user's existing JWTs return **401
  ("Account deactivated")** on the next request.
- A `user_purge` async job is scheduled at `scheduled_purge_at` (default 30
  days). When that fires, the user's data is hard-deleted (CASCADE wipes
  wardrobe items, settings, sessions, daily recommendations).
- The `audit_log` survives the purge for compliance evidence.

🔶 **iOS**: clear local storage immediately. Tokens are dead, account is
"gone" from the app's perspective. Show a deactivation-success screen and
remind users about the 30-day reactivation window.

### 7.2 Reactivation

A soft-deleted user **can come back**: another Apple sign-in via
`POST /v1/auth/login/apple` within the grace window auto-reactivates the
account (clears `deactivated_at`). The pending hard-purge job detects the
reactivation and cancels itself when it fires.

After `purged_at` is set, the user is gone forever — re-signing-in via
Apple at that point creates a brand-new account that happens to share the
Apple sub.

🔶 **iOS**: don't show a "reactivate" prompt explicitly — Apple-login Just
Works. But surface a toast when login returns a previously-deactivated
account so the user knows the data they "deleted" is back.

### 7.3 DSAR data export

`POST /v1/me/data-export` enqueues an async job that bundles all of the
user's data and surfaces a download link.

Response:
```json
{ "data": { "job_id": "<uuid>", "status": "pending", "message": "..." } }
```

Idempotency: at most one in-flight export per user. Calling again before
completion returns the same job_id. After completion (terminal status),
the next call triggers a fresh export. Per-day idempotency: on a given
day, repeated requests return the same job until it terminates.

🔶 **iOS**: surface the job_id; tell the user they'll get a notification
when ready (V4 will add explicit status polling endpoint).

---

## 8. Cost summary

`GET /v1/user/cost-summary` returns the user's daily quota usage:

```json
{
  "data": {
    "llm_ask":     { "used": 12500, "limit": 50000, "remaining": 37500 },
    "ocr_receipt": { "used": 0,     "limit": 100,   "remaining": 100 },
    "tryon":       { "used": 1,     "limit": 5,     "remaining": 4 }
  }
}
```

`llm_ask` is token-based (counts SUM of cost_tokens over jobs that incurred
cost — including failed/dead, since providers bill on attempt). Others are
call-count (only succeeded/running counted; failed retries don't burn quota).

When `ENABLE_COST_GUARD=false`, the endpoint still returns the structure
with `limit` set to the configured default. Useful for pre-rendering UI
before the operator turns enforcement on.

🔶 **iOS**: when `remaining=0` and the user attempts a cost-bearing action,
the relevant endpoint returns **429** (`error_code: 4091`) with the message
"daily {job_type} quota exhausted". Show a friendly explanation and refer
to the cost-summary screen.

---

## 9. Admin observability (operators only)

`GET /v1/admin/jobs?status=dead&job_type=&limit=&offset=` — paginated view of
async-job state. Gated by `ADMIN_USER_IDS` allowlist (config-side, not
exposed to clients).

Not for iOS app code; useful for an internal ops dashboard if you build one.
Response includes `job_id`, `job_type`, `status`, `attempts`, `last_error`,
`correlation_id`, `cost_tokens`, `cost_cents`, lifecycle timestamps.

---

## 10. Error shape

Unchanged from V3. Recap:

```json
{ "code": 4001, "message": "...", "data": null }
```

| Code | HTTP | Meaning |
|---|---|---|
| 4001 | 422 | Validation error |
| 4002 | 401 | Unauthorized |
| 4003 | 403 / 503 | Forbidden / kill-switch |
| 4041 | 404 | Not found |
| 4091 | 409 / 429 | Conflict / rate-limited |
| 5001 | 500 | Internal error |

🔶 **iOS**: 429 = retry with backoff (or surface "you're moving too fast").
401 with message "Account deactivated" = log the user out, redirect to
sign-in. 503 = feature flag disabled — should never happen for shipping
clients but useful in canary/rollback.

---

## 11. Migration checklist for iOS

Prioritized — top items unblock the most surface, bottom items are polish.

1. **Auth**: handle the new 401 reason "Account deactivated" — log out + redirect.
2. **Image lifetimes**: stop persisting `image_url` past session boundaries. Refresh from the parent endpoint instead.
3. **Refresh button** (when `ENABLE_TODAY_REFRESH` ships): wire `POST /today/refresh` + handle 429 + invalidate local cache.
4. **Reason tag dictionary**: pull `/feedback-tags?scene=` once per scene and cache for the session.
5. **Bundle feedback**: replace per-tap `/feedback` calls with a single `/feedback/bundle` when the user submits both overall + per-item.
6. **Replacement suggestions**: render the first wearable id from `obstacle.replacement_candidate_ids` when an outfit shows a `laundry` / `lost` / `seasonal` obstacle.
7. **i18n fallback**: for every I18nMessage, attempt `key` lookup first then `fallback_text` — never assume key is registered.
8. **STS upload**: replace the legacy presign-upload path with `/uploads/sts-token` + direct-to-OSS PUT for new uploads (keep presign as fallback for clients that can't load the OSS SDK).
9. **Cost summary screen**: surface from `/user/cost-summary` (also drives the 429 explanation).
10. **Data export + delete account screens**: implement `DELETE /v1/me` and `POST /v1/me/data-export` flows in account settings.

---

## 12. Open items / V4 candidates

These are spec'd but not yet shipped — don't depend on them in the next
iOS release:

- **Try-on body-image flow** (private bucket, 30-day auto-purge)
- **LLM Ask sessions** (will use the cost_guard token quota)
- **OCR receipt parsing** (call-count quota)
- **PDF ingestion** (async job)
- **Data export status polling endpoint** (`/v1/me/data-export/{job_id}`)
- **CDN-signed URLs** (currently OSS-direct signed; CDN switch is operator-side, no API change)

---

## Reference

- Original V3 contract: [`ios-api-alignment.md`](./ios-api-alignment.md)
- Async-jobs framework (mostly server-internal): [`async-jobs.md`](./async-jobs.md)
- Backend evolution plan: [`backend-gaps-plan.md`](./backend-gaps-plan.md)
- OpenAPI spec (regenerate via `make openapi`): [`openapi.yaml`](./openapi.yaml) — single source of truth for shapes
