# Item cutout — frontend contract

After an item is imported, the backend asynchronously generates a clean
product-shot cutout (gpt-image-2) and swaps it in as the item cover. This is a
**background progressive enhancement** — never block the user on it.

## The field

Every wardrobe item carries `cutout_status` (in both `GET /v1/wardrobe-items`
list and `GET /v1/wardrobe-items/{item_id}`):

| value | meaning | frontend |
|-------|---------|----------|
| `skipped` | no cutout (feature off, or no source image) | show `cover_image_url` as-is, no badge |
| `queued` | cutout is being generated | show `cover_image_url` (= original photo) + subtle "精修中" badge; poll |
| `ready` | done — `cover_image_url` now points at the cutout | show `cover_image_url`, drop the badge |
| `failed` | generation gave up (dead-lettered) | show `cover_image_url` (original), drop the badge; no retry needed |

`cover_image_url` is **always renderable**: it's the original photo while
`queued`, and is replaced in place by the cutout on `ready`. There is never a
blank/loading cover.

## UX rules

1. **Do not block import.** On confirm, go straight to the wardrobe. The item
   appears immediately with its original photo.
2. **No full-screen spinner, no countdown.** At most a small corner badge
   ("精修中" / a sparkle) on items with `cutout_status === 'queued'`.
3. **Poll while queued.** Every 3–5s, refetch the item (or the list) and read
   `cutout_status`. On `ready`, swap the thumbnail to the new `cover_image_url`.
4. **Timeout fallback.** Stop polling after ~90s; keep the original photo. The
   backend's dead-letter reconcile will flip stuck items to `failed` on its own.
5. **User can leave.** No need to stay on screen — next time they view the grid
   the cover is already the cutout.

## Timing

- Typical end-to-end: **~20–40s** (≤5s queue wait + ~20–35s gpt-image-2 gen).
- The gen time is a hard floor (model), so treat this as fire-and-forget, not a
  wait the user watches.

## Example

`GET /v1/wardrobe-items/{id}` (envelope omitted) right after import:

```json
{ "item_id": "…", "title": "Off-Shoulder Top", "category": "top",
  "cover_image_url": "https://…/wardrobe/…/original.jpg",
  "cutout_status": "queued" }
```

…and after ~30s:

```json
{ "item_id": "…", "title": "Off-Shoulder Top", "category": "top",
  "cover_image_url": "https://…/{user}/items/{item}/cutout.png",
  "cutout_status": "ready" }
```

Poll suggestion: only poll items whose last-seen `cutout_status === 'queued'`;
once none remain queued, stop.
