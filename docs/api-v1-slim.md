# V1 Slim API

All regular API responses use:

```json
{
  "code": 0,
  "message": "ok",
  "data": {}
}
```

Temporary development auth uses `X-User-Id: <uuid>`. This is disabled when `APP_ENV=prod`.

## Health

- `GET /healthz`

## Settings

- `GET /v1/user/settings`
- `PATCH /v1/user/settings`

```json
{
  "departure_time": "08:30:00"
}
```

## Uploads

- `POST /v1/uploads/presign`

Only image content types are accepted: `image/jpeg`, `image/png`, `image/webp`.
`max_content_length` is capped at 10MB.

```json
{
  "file_name": "shirt.jpg",
  "content_type": "image/jpeg",
  "biz_type": "wardrobe_item"
}
```

## Import Tasks

- `POST /v1/import-tasks`
- `GET /v1/import-tasks/{task_id}`
- `POST /v1/import-tasks/{task_id}/confirm`

Only `input_type=image` is implemented in V1 Slim. Link, share, screenshot, retry, and callback flows are intentionally out of scope.

## Wardrobe

- `GET /v1/wardrobe-items`
- `GET /v1/wardrobe-items/{item_id}`
- `PATCH /v1/wardrobe-items/{item_id}`
- `DELETE /v1/wardrobe-items/{item_id}`

Status values are limited to `wearable` and `unavailable`.

## Recommendations

- `GET /v1/recommendations/today`
- `GET /v1/recommendations/outfits/{outfit_id}`
- `POST /v1/recommendations/outfits/{outfit_id}/wear`
- `POST /v1/recommendations/outfits/{outfit_id}/feedback`

`GET /v1/recommendations/today` must never return a loading-only state or `generating`. If no cached recommendation exists, it returns `insufficient_items` guidance.

