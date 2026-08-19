# V3 P0 Cross-Repo Contract (Frozen)

> **Status: FROZEN for P0 — deliverable of V3-001.** This is the single contract
> document for V3 P0 API boundaries, feature flags, enums, error semantics,
> visual asset states and rollback behavior. The canonical copy lives at
> `aidrobe-BE/docs/v3-p0-contract.md`; `aidrobe-FE/docs/v3-p0-contract.md` must
> stay **byte-identical** (same convention as `docs/openapi.json`). Any revision
> must update both repos inside the same feature and state the change in the PR.
>
> Authority order: `V3_SPEC.md` → this contract → implementation. Where this
> contract pins a name or shape, later features (V3-002 … V3-023) implement
> exactly this shape. FE and BE never independently invent or reinterpret a
> field defined here.

## 1. Scope and relationship to existing surfaces

P0 delivers three bounded capabilities (V3_SPEC §16):

1. **Purchase decision** — structured Should I Buy judgment grounded in the
   user's real wardrobe (V3-003 … V3-010).
2. **Avatar profile** — consent-gated person reference built from naturally
   produced photos (V3-011 … V3-015, V3-021).
3. **Static try-on preview** — one on-demand L2 static full-outfit preview per
   decision (V3-016 … V3-020).

**Additive-only rules (frozen):**

- `POST /v1/shopping/sense-check` and `GET /v1/shopping/history` (M17) stay
  untouched in P0. All V3 endpoints are new surfaces — with one deliberate
  exception: `GET /v1/outfit-visual-assets/{asset_id}` is the **existing M10
  outfit-board polling endpoint**, extended additively (§6.2); it is never
  re-gated or re-shaped. Nothing existing is removed, renamed or re-shaped.
- `outfit_visual_assets` is **extended with additive, nullable columns**
  (V3_SPEC §11.3); no existing column changes meaning. Migrations are additive
  and reversible.
- No new navigation tab. V3 entries appear only inside existing surfaces
  (Outfit Detail, Ask answer actions, Should I Buy result, Profile data
  controls) per V3_SPEC §3.3 / §4.
- V2 core paths (Today, import-to-first-outfit, Ask) must be byte-identical in
  behavior while every V3 flag is off.

## 2. Feature flags

Convention: pydantic-settings `ENABLE_*` booleans in
`app/config/settings.py`, **all default `False`** (existing repo convention).

| Flag | Gates | Depends on |
|---|---|---|
| `ENABLE_V3_PURCHASE_DECISION` | `/v1/purchase-candidates*`, `/v1/wardrobe-decisions/{id}` | — |
| `ENABLE_V3_AVATAR` | `/v1/avatar-profile*`, `/v1/avatar-candidates/*/consent` | — |
| `ENABLE_V3_STATIC_TRYON` | `POST /v1/wardrobe-decisions/{id}/visual-preview`, `POST /v1/outfit-visual-assets/{asset_id}/feedback` — **new surfaces only**; the existing `GET /v1/outfit-visual-assets/{asset_id}` (M10) stays ungated | `ENABLE_V3_AVATAR` |

**Rollback semantics (frozen):**

1. Flag off ⇒ every gated endpoint raises
   `FriendlyException(error_codes.FORBIDDEN /* 4003 */, "<feature> is not enabled", status_code=503)`
   — identical to the existing `ENABLE_PACKING_LIST` gate.
2. FE: every V3 surface is an **optional, self-hiding** entry — on a 503 from
   its probe/query it hides silently (existing `src/api/client.ts` convention
   for feature-flagged cards). No toast, no retry loop, no placeholder.
3. Data written while a flag was on **persists** when the flag turns off
   (`ShoppingSenseCheck` M17 precedent: the flag gates exposure only). Turning
   a flag back on restores function with no migration.
4. If `ENABLE_V3_STATIC_TRYON` flips off with jobs in flight: running jobs
   finish or fail normally, no new jobs enqueue, and cached assets remain
   readable via the existing ungated `GET /v1/outfit-visual-assets/{asset_id}`.
   Flag state must never affect that GET — V2 outfit-board polling depends
   on it (§1 additive-only rules).
5. `ENABLE_V3_STATIC_TRYON=true` with `ENABLE_V3_AVATAR=false` is an invalid
   configuration: BE fails at startup via the settings `model_validator`
   (`"ENABLE_V3_STATIC_TRYON requires ENABLE_V3_AVATAR"`), matching the
   existing `ENABLE_PACKING_PLANNER requires ENABLE_PACKING_LIST` precedent.

## 3. Wire conventions

- JSON wire format is **snake_case**; FE maps to camelCase inside `src/api/*`
  (existing convention, e.g. `push_enabled` → `pushEnabled`).
- Success responses are plain Pydantic response models (HTTP 200/202).
  Errors use the unified envelope `{code, message, data}` produced by the
  exception middleware. No new top-level error codes: reuse
  `4001 VALIDATION_ERROR`, `4002 UNAUTHORIZED`, `4003 FORBIDDEN`,
  `4041 NOT_FOUND`, `4091 STATE_CONFLICT`, `5001 INTERNAL_ERROR`.
- Resources that exist but are not owned by the caller return `NOT_FOUND`
  (no existence leak).
- All IDs are UUID strings. All timestamps are ISO-8601 UTC.
- Image URLs in responses are **short-lived signed URLs**, never persisted by
  the client (V3_SPEC §13.5).

**Version-skew rule (frozen):** FE must degrade safely on unknown values —
unknown `avatar_status` ⇒ treat as `disabled` (hide avatar entries); unknown
`generation_status` ⇒ treat as `failed` (show the standard no-result fallback);
unknown `decision` ⇒ render server-provided texts without decision-specific
styling or actions. A response missing an optional V3 block means "feature not
available", never an error.

## 4. Avatar contract

### 4.1 `avatar_status` state machine (frozen enum)

```text
none                  no candidate material
candidate             qualified material exists, not yet consented
preparing             consented; canonical reference assets being generated
ready                 usable for static try-on
refresh_recommended   usable; a newer/better source is suggested
disabled              user revoked or switched off
```

Transitions: `none→candidate` (quality check passes on a natural photo),
`candidate→preparing` (explicit consent), `preparing→ready` (preparation job
succeeds), `preparing→candidate` (preparation fails; source stays consentable),
`ready→refresh_recommended` (system suggestion only, never a nag),
`any→disabled` (revoke), `disabled→preparing` (user re-enables via new consent).

### 4.2 Endpoints

| Method + path | Purpose |
|---|---|
| `GET /v1/avatar-profile` | Current profile state. `status=none` is a normal 200, not 404. |
| `POST /v1/avatar-candidates/{source_asset_id}/consent` | Accept or decline one candidate. |
| `POST /v1/avatar-profile/uploads` | Register a deliberately uploaded full-body photo (source C). Reuses the existing presign upload flow; body carries the uploaded asset reference. |
| `POST /v1/avatar-profile/prepare` | Start preparation from a consented source. |
| `POST /v1/avatar-profile/replace` | Prepare a new version; atomic swap on success. |
| `DELETE /v1/avatar-profile?purge_derived=<bool>` | Disable and delete. `purge_derived` is **required** (no default): `true` also deletes generated try-on assets derived from any avatar version. |

`GET /v1/avatar-profile` response (frozen shape):

```jsonc
{
  "status": "none|candidate|preparing|ready|refresh_recommended|disabled",
  "active_version": {            // null unless status is ready/refresh_recommended
    "id": "uuid",
    "created_at": "ts",
    "canonical_front_url": "signed-url"
  },
  "candidate": {                 // null unless status is candidate
    "source_asset_id": "uuid",
    "source_type": "ootd_photo|outfit_import|user_upload|camera_capture",
    "preview_url": "signed-url"
  },
  "consent": {                   // null until first consent
    "version": "string",         // consent copy version, audit-relevant
    "consented_at": "ts"
  }
}
```

`POST …/consent` body: `{"accept": true|false}`.
`accept=false` dismisses that candidate permanently — the same asset is never
re-prompted (V3_SPEC §7.3). `accept=true` requires the consent copy version in
`{"accept": true, "consent_version": "string"}` and moves the profile to
`preparing`.

Conflict rules: `prepare`/`replace` while a preparation job is running ⇒
`4091 STATE_CONFLICT`. `replace` failure keeps the previous version active —
there is never a gap where a ready avatar silently disappears.

### 4.3 Consent boundaries (frozen minimums)

- Consent is a **separate explicit action per source asset**; importing
  clothes never implies person-reference consent (source B rule, V3_SPEC §7.1).
- Revoke (`disabled`) immediately blocks new generation; existing decisions
  remain readable.
- Final consent/privacy copy is a **human gate** — implementation uses
  placeholder copy keys, and shipping copy requires explicit human approval.

## 5. Purchase decision contract

### 5.1 Endpoints

| Method + path | Purpose |
|---|---|
| `POST /v1/purchase-candidates` | Create a candidate from a link/screenshot/order image. |
| `GET /v1/purchase-candidates/{id}` | Poll parsing state. |
| `POST /v1/purchase-candidates/{id}/decision` | Produce (or return the existing) structured decision. |
| `GET /v1/wardrobe-decisions/{id}` | Replay a decision from its immutable snapshots. |

### 5.2 Candidate

`POST /v1/purchase-candidates` body (exactly one source reference):

```jsonc
{
  "source_type": "share_link|screenshot|order_image|ask_link",
  "source_url": "https://…",     // for share_link / ask_link
  "source_asset_id": "object_key" // for screenshot / order_image (presign upload)
}
```

**`source_asset_id` is the presign `object_key` string, not a uuid** (amended in
V3-004; this document previously typed it `uuid`). This service has no generic
asset table — `POST /v1/uploads/presign` returns a stable `object_key` plus a
TTL-bound `file_url`, so the object key *is* the asset reference and is the
column `purchase_candidates.source_object_key` persists (V3-003). The key must
sit under the caller's own `users/<user_id>/` prefix; anything else answers 404,
not 403, so the response cannot probe which keys exist (§3). Nothing signed is
ever persisted.

Response / `GET` shape: `{"id", "status", "parsed_item"}` with
`status: "submitted|parsed|parse_failed"` (frozen enum). Parsing may complete
synchronously or async; FE polls `GET` until terminal. `parse_failed` still
allows a decision request — the decision then returns `insufficient_information`
with a clarifying reason instead of a hard error (graceful degradation).

As implemented in V3-004 parsing is **synchronous**, so `POST` already returns a
terminal `parsed` or `parse_failed` and `submitted` is not observable through
this endpoint; the value stays in the frozen enum for a later async parse. FE
(V3-008) must therefore treat polling as optional-and-usually-unnecessary rather
than build a loop that expects a non-terminal state — but it must still handle
`submitted`, and any unknown status, without spinning forever (§3 version-skew
rule).

`parsed_item` (frozen minimal shape, all fields nullable):

```jsonc
{
  "category": "string",
  "colors": ["string"],
  "silhouette": "string",
  "material_hints": ["string"],
  "price": "string",
  "scene_tags": ["string"]
}
```

### 5.3 Decision

`POST …/{id}/decision` body: `{"concern": "volume|color|redundancy|scene|none"}`
(optional, default `none`). Repeated calls for the same candidate return the
existing decision (idempotent) unless the wardrobe changed materially — P0 may
simply always return the first decision.

`wardrobe_decision` response (frozen shape):

```jsonc
{
  "id": "uuid",
  "candidate_id": "uuid",
  "decision": "worth_adding|consider_alternate|wait|avoid_duplicate|insufficient_information",
  "decision_text": "string",           // display copy; FE never synthesizes it
  "reasons": [                          // 2–3, ordered by importance
    {
      "kind": "redundancy|compatibility|scene_coverage|extra_purchase_needed|adoption_history",
      "text": "string"
    }
  ],
  "closet_comparison": {
    "summary_text": "string",
    "similar_items": [
      { "item_id": "uuid", "image_url": "signed-url", "relation": "covers|complements" }
    ]
  },
  "uncertainty": "low|medium|high",
  "visual_need": "none|helpful|recommended",   // drives preview entry weight
  "boundary_note": "string",                    // fixed copy: not a size/fit guarantee
  "next_actions": ["add_to_candidates","skip","recheck_color","style_with_existing"],
  "created_at": "ts"
}
```

Frozen rules:

- Enum literals in this document are adopted **verbatim from the immutable
  acceptance criteria in `feature_list.json`** (V3-007 decision states,
  V3-020 feedback verdicts), which outrank this contract in the workspace
  authority order. `visual_need: "none"` is the "visual need is false" state
  V3-007 requires; `helpful|recommended` only grade the entry weight.
- `decision` has exactly these five states — **no numeric suitability score**
  (V3_SPEC §5.4).
- `context_snapshot` and `evidence_snapshot` are persisted server-side at
  decision time; `GET /v1/wardrobe-decisions/{id}` replays from snapshots so
  later wardrobe changes never rewrite an old explanation.
- The full no-avatar experience is complete: nothing in this shape depends on
  avatar or visual preview. `visual_need` only *suggests* whether a preview
  entry is worth showing.

As implemented in V3-007 (clarifications, no shape change):

- The endpoint answers **HTTP 200**, never 201, because the second call returns
  the first decision — a created-status would be wrong half the time (§3 allows
  200/202). The request body is **optional**: an omitted body means
  `concern: "none"`, and an unrecognized `concern` is a 422.
- Idempotency is enforced by the unique index on `wardrobe_decisions.candidate_id`,
  so a repeated POST replays the stored snapshot and **ignores a new `concern`**.
  P0 never re-decides a candidate.
- `reasons[].kind` may be any of the five frozen values, but P0 emits four:
  **`adoption_history` is not produced yet** (no loaded projection carries wear
  counts). FE must tolerate it without building a surface that depends on it.
- `closet_comparison.similar_items[].image_url` is signed at read time and is
  **nullable** — an item with no image, or a storage outage, renders `null`
  rather than failing the decision. It is never persisted in the snapshot.
- `decision_text`, `reasons[].text`, `closet_comparison.summary_text` and
  `boundary_note` are server-rendered from templates in the user's resolved
  locale (`zh-CN` / `en-US`) and **frozen into the snapshot** at decision time,
  so a replayed decision keeps the wording — and the language — it was written
  with. There is no i18n key in this shape; the client renders the strings.

## 6. Static try-on preview contract

### 6.1 Endpoints

| Method + path | Purpose |
|---|---|
| `POST /v1/wardrobe-decisions/{id}/visual-preview` | New. Request (or fetch cached) static try-on for this decision. |
| `GET /v1/outfit-visual-assets/{asset_id}` | **Existing M10 endpoint** (outfit-board polling), extended additively — never gated or re-shaped. |
| `POST /v1/outfit-visual-assets/{asset_id}/feedback` | New. Record user fidelity feedback. |

`POST …/visual-preview` body (frozen):

```jsonc
{ "view_type": "static_tryon", "concern": "volume|color|length|overall", "consent": true }
```

BE selects supporting items (server-side outfit composition); FE submits no
open-ended styling state in P0 (V3_SPEC §12). Responses:

- Avatar not `ready`/`refresh_recommended` ⇒ `4091 STATE_CONFLICT`.
- Cache hit (see 6.3) ⇒ 200 with the existing asset, `generation_status`
  possibly already `ready`. Identical inputs never regenerate.
- Otherwise ⇒ 200 `{"asset_id": "uuid", "generation_status": "queued"}`.

### 6.2 `generation_status` lifecycle (frozen enum)

```text
queued → processing → quality_check → ready
                          │
                          ├→ failed                      (provider/technical error)
                          └→ rejected_by_quality_gate    (gate says: do not show)
```

`GET /v1/outfit-visual-assets/{asset_id}` — the live M10 shape is kept
verbatim and only extended additively (frozen):

```jsonc
{
  "asset_id": "uuid",            // existing field name — NOT "id" (ios-api-alignment convention)
  "generation_status": "queued|processing|quality_check|ready|failed|rejected_by_quality_gate",
  "image_url": "signed-url",     // existing; null unless ready
  "visual_type": "static_tryon", // existing; new enum value for V3 assets
  "error_code": "string",        // existing; null unless failed
  "error_message": "string",     // existing; kept, null unless failed
  "created_at": "ts",            // ADDITIVE, nullable
  "expires_at": "ts"             // ADDITIVE, nullable
}
```

`quality_check` and `rejected_by_quality_gate` are **additive enum values** on
the existing `generation_status` field; FE versions that predate them degrade
per the §3 version-skew rule (unknown status → `failed`-equivalent display).

**Frozen degradation rule:** FE renders `rejected_by_quality_gate` with exactly
the same user-facing copy and layout as `failed` — the technical distinction is
never exposed. Both keep the decision result fully intact (V3_SPEC §3.6, §9.5).
Low-quality results are hidden, never shown.

Feedback body: `{"verdict": "looks_like_me|not_like_me|garment_wrong",
"aspects": ["face","body_ratio","garment_fit","color","other"]}` (aspects
optional).

As implemented in V3-024 (clarifications, no change to the frozen body):

- The endpoint answers **HTTP 200** on every accepted submission, including a
  repeat — the first tap and an identical re-tap describe the same recorded
  state, so a 201/200 split would make a retry look like a different outcome.
  Success shape (not previously frozen, so this is the definition):
  `{"asset_id": "uuid", "verdict": "…", "aspects": [...] | null,
  "recorded_at": "ts"}`. It deliberately carries **no** `provider` /
  `model_version` / `prompt_version` — §6.4 keeps that provenance internal, and
  the evaluation joins it server-side instead.
- `aspects` omitted and `aspects: []` stay distinct: "did not elaborate" is not
  "chose nothing". Submitted aspects are sorted and deduplicated server-side,
  so re-tap order can never register as a change of answer. Any value outside
  the two vocabularies — or any extra field, including a free-text comment — is
  **422** (`extra="forbid"`, the §5.2/V3-019 precedent).
- **404, three ways, one answer**: a missing asset, another user's asset, and
  the caller's own non-`static_tryon` asset are indistinguishable on the wire.
  The first two are §3's no-existence-leak rule; the third keeps this from
  becoming a generic image-feedback endpoint. `GET /v1/outfit-visual-assets/
  {asset_id}` stays ungated (§2) — only this POST is behind
  `ENABLE_V3_STATIC_TRYON`, which answers **503** when off, before the body is
  inspected.
- **One verdict per render, last write wins.** `static_preview_feedback` is
  unique on `asset_id`, and the write is an upsert onto it: an identical
  resubmission writes nothing at all (same row id, same `recorded_at`), and a
  *conflicting* resubmission replaces the stored verdict and moves
  `recorded_at`. This is §6.1's "either explicitly rejected or deterministically
  replace according to one documented rule" — replace is the rule, chosen
  because the client surface is fire-and-forget, so a rejection would be
  invisible to a user who believes their new answer was recorded.
- The BE emits **no** analytics event here. `v3_preview_feedback` is owned by
  **fe** (`docs/v3-analytics-events.md` §2), and a second emit would
  double-count every rate that reads it.

### 6.3 Idempotency, cache and cost (frozen)

- Strong cache key:
  `(avatar_version_id, normalized_candidate_asset_hash, sorted_supporting_item_ids, view_type, provider_model_version, prompt_version)`
  — same key ⇒ same asset, no regeneration (V3_SPEC §13.1).
- Job idempotency key: `static_tryon:{sha256(cache_key)}` following the
  existing `app/infrastructure/workers` job-runtime conventions.
- Automatic retry: **at most one** model retry after a quality-gate failure,
  then `rejected_by_quality_gate`. Generation runs under the existing
  `ENABLE_COST_GUARD` per-user-per-day quota mechanism.
- `visual_type` values added in P0: `static_tryon`, `avatar_canonical`
  (`outfit_multiview` is reserved for P1 and must not appear in P0 responses).

### 6.4 Provider ports (names frozen)

```text
AvatarPreparationPort
StaticTryOnPort
VisualQualityGatePort
MultiViewGenerationPort   (reserved, P1 — not implemented in P0)
```

Providers (MiniMax or others) are adapters behind these ports and never leak
into the domain contract or API responses. `provider`, `model_version`,
`prompt_version` are internal columns, not exposed to FE. Development and CI
use fake/recorded adapters only; any real-provider evaluation is a separately
budgeted, explicitly authorized task (V3-016, human gate). Real user photos
are never sent to third-party providers during development loops.

## 7. Privacy and logging boundaries (frozen minimums)

- Person photos are high-sensitivity assets: independent consent, visible
  status, replace, disable and delete controls in Profile (V3_SPEC §15).
- Logs and analytics must never contain image bytes, signed URLs, or body /
  identity inferences.
- No output may state or imply size or fit accuracy; every preview carries the
  fixed boundary note.
- No body-shape commentary of any kind (no slimming, age, attractiveness or
  "fix" language).

## 8. OpenAPI process (frozen)

- V3-001 introduces **no OpenAPI change**; `docs/openapi.json` stays
  byte-aligned FE↔BE (`./init.sh contract`).
- Every later BE feature that touches routers/schemas runs `make openapi` and
  copies `docs/openapi.{json,yaml}` to the FE repo **in the same feature**, so
  `./init.sh contract` is green at every merge point.
- Analytics/event schemas are defined by V3-002 and must not modify the API
  shapes frozen here.

## 9. P0 scope exclusions (frozen)

P0 explicitly excludes (V3_SPEC §16, §21): each item below is out of scope and
its absence is an acceptance criterion, not an omission.

| Excluded | Contract consequence |
|---|---|
| Multi-view / 360° / video | `view_type` accepts only `static_tryon`; `outfit_multiview` never appears. |
| 3D mesh / AR / cloth physics | No such fields or claims anywhere. |
| Size / fit accuracy claims | `boundary_note` mandatory; no fit scores exist in any shape. |
| Mandatory photo onboarding | All avatar acquisition is contextual and declinable; decline keeps the full decision experience. |
| New top-level tab | V3 has no navigation entry; only in-context surfaces. |
| Infinite try-on / outfit editor | One preview per decision; BE picks supporting items; no FE styling submission. |
| Generation-count gamification | No quotas, counters or streaks in any response shape. |
| Avatar identity homepage | Profile exposes data controls only. |
| Body scoring / appearance judgment | Forbidden output categories per §7 above. |
