# iOS API Alignment Guide — V3

## Authentication

Two modes depending on `APP_ENV`:

| Env | Method | Header |
|-----|--------|--------|
| `prod` | Bearer JWT | `Authorization: Bearer <token>` |
| `dev` | User-ID bypass | `X-User-Id: <uuid>` |

Login flow: `POST /v1/auth/login/apple` → returns `access_token` + `refresh_token`.  
Refresh: `POST /v1/auth/refresh` with `{ "refresh_token": "..." }`.

---

## Response envelope

All existing endpoints (wardrobe, recommendations, imports) wrap responses in:

```json
{ "data": { ... } }
```

**V3 new endpoints** (insights, outfit-history, outfit-visual) return the data **directly** (no envelope). iOS should handle both patterns.

---

## Pagination — two conventions (known inconsistency)

| Router | Style | Params | Response fields |
|--------|-------|--------|-----------------|
| `GET /v1/wardrobe` | limit/offset | `?limit=20&offset=0` | `limit`, `offset`, `total` |
| `GET /v1/outfit-history` | page/page_size | `?page=1&page_size=20` | `page`, `page_size`, `total` |

Recommendation: iOS should support both until unified in a future refactor.

---

## UUID field naming — known inconsistency

| Endpoint | Primary key field |
|----------|------------------|
| `WardrobeItemResponse` | `item_id` |
| `OutfitResponse` | `outfit_id` |
| `OutfitHistoryEntry` | `wear_history_id` |
| `OutfitVisualAssetResponse` | `asset_id` |
| `InsightsSummaryResponse` | *(no id)* |

All UUIDs are lowercase hyphenated strings: `"3fa85f64-5717-4562-b3fc-2c963f66afa6"`.

---

## Date / datetime formats

- `date` fields: `"YYYY-MM-DD"` (e.g. `"2026-05-05"`)
- `datetime` fields: ISO 8601 with timezone `"2026-05-05T08:30:00+00:00"`
- All timestamps are UTC.

---

## V3 new endpoints

### Outfit History (M11)

```
GET  /v1/outfit-history                    List history (page/page_size)
GET  /v1/outfit-history/calendar           Dates with records (?year=&month=)
GET  /v1/outfit-history/{date}             Single day detail (YYYY-MM-DD)
POST /v1/outfit-history/{id}/ootd          Upload OOTD { "ootd_image_url": "..." }
```

### Wardrobe Insights (M12)

```
GET  /v1/insights/summary
GET  /v1/insights/top-items                ?limit=10
GET  /v1/insights/idle-items               ?threshold_days=60
GET  /v1/insights/color-palette
GET  /v1/insights/style-trend
GET  /v1/insights/cost-per-wear
```

### Outfit Visual Assets (M10)

```
GET  /v1/outfit-visual-assets/{asset_id}   Board generation status
GET  /v1/wardrobe-items/{item_id}/images   All image types for an item
```

### Recommendations — V3 additions (M13)

`GET /v1/recommendations/today` now includes `style_context`:

```json
{
  "data": {
    "outfits": [...],
    "style_context": {
      "template_id": "minimal_commute_v1",
      "active_tags": ["black", "white", "minimal"],
      "confidence": 0.72
    }
  }
}
```

`style_context` may be `null` when the user has no style profile yet.

### Wardrobe — V3 additions (M14)

`WardrobeItemResponse` now includes optional fields:

```json
{
  "item_id": "...",
  "sub_category": "shirt",
  "color_secondary": "navy",
  "material": "cotton",
  "season_tags": ["spring", "autumn"],
  "style_tags": ["minimal", "casual"],
  "formality_score": 2,
  "time_cost_level": "low",
  "purchase_price": 299.00,
  "wear_count": 5
}
```

All new fields are `null` by default. `formality_score` is 1–5 (1=极休闲, 5=正式).

---

## Error response format

All errors use `FriendlyException` and return:

```json
{
  "error_code": 4041,
  "message": "Wardrobe item not found"
}
```

Common codes:

| Code | Meaning | HTTP Status |
|------|---------|-------------|
| 4001 | Validation error | 422 |
| 4002 | Unauthorized | 401 |
| 4003 | Forbidden | 403 |
| 4041 | Not found | 404 |
| 4091 | State conflict | 409 |
| 5001 | Internal error | 500 |

---

## Known gaps / TODO before iOS integration

1. **`wear_count`** is now incremented on every `POST /v1/recommendations/outfits/{id}/wear`. Existing items in DB have `wear_count=0` — a backfill migration is needed for production data.
2. **Outfit Board** (`/v1/outfit-visual-assets`) — generation is async (arq worker). iOS should poll `generation_status` until `"completed"` or `"failed"`.
3. **`style_context.template_id`** drives UI theming. Current templates: `minimal_commute_v1`, `soft_feminine_v1`, `formal_office_v1`, `casual_street_v1`, `general_v1`.
4. **Pagination unification** — `wardrobe` uses `limit/offset`, `outfit-history` uses `page/page_size`. Will be unified in a future pass.

## 衣物导入任务

`GET /v1/import-tasks` 省略 `status` 时只返回 `waiting_confirm`。重复传参可查询多个状态，例如 `?status=processing&status=failed`。分页继续使用 `limit` 和 `offset`，同一时间按任务 ID 稳定排序。

`POST /v1/import-tasks/{task_id}/retry` 只处理当前账户的任务。可重试失败会以同一个任务 ID 返回 `queued`。进行中重复调用返回当前状态，已识别或永久失败返回 409。失败响应的可选 `retryable` 字段供客户端决定是否显示重试入口。

`candidate_item` 对应固定首候选，与旧 `/confirm` 一致。重复确认同一候选返回原衣物 ID，分批确认保留其余候选。若要关闭未确认候选，可在 `/confirm-items` 传 `dismiss_remaining=true`。裁剪可能在确认之后到达，重新读取候选或衣物可获取最新图片。私有图片 URL 应在展示时读取更新。

首套接口继续返回 HTTP 200 和 `no_outfit`、`single_outfit`、`ready` 三种业务状态。首套生成超时沿用错误响应。现有客户端可继续使用原流程，新增状态查询和重试入口由客户端另行接入。
