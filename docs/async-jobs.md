# Async Jobs Framework

Backend reference for the `async_jobs`-backed runtime introduced in Phase C
of `docs/backend-gaps-plan.md`. Every long-running, retryable, or
cost-incurring async pipeline MUST go through `app/infrastructure/workers/job_runtime.py`.

## State machine

```
queued ─claim_one─▶ running ─mark_succeeded─▶ succeeded
                       │  ─mark_failed (attempts<max)──▶ queued (with backoff)
                       │  ─mark_failed (attempts>=max)─▶ dead
                       └─lease expires────▶ recover_expired_leases
                                              ├─ attempts<max  → queued
                                              └─ attempts>=max → dead
```

- `claim_one` increments `attempts` and **commits** so a worker crash
  doesn't roll the row back to `queued` and erase retry accounting.
- `mark_*` are guarded by `status='running'` and the claimed attempt so stale finalizers
  cannot resurrect terminal jobs.
- `dead` is terminal. Sentry pages on every transition into `dead`
  (both `mark_failed` and lease-recovery paths).

## Idempotency key conventions

Producers MUST construct keys so re-enqueueing the same logical work returns
the existing job rather than creating a second one.

| `job_type`     | key format                                | rationale |
|----------------|-------------------------------------------|-----------|
| `embedding`    | `embedding:{item_id}:{sha256(model_version + NUL + source_version)}` | One job per item, current true source and model. Writes upsert the current row after checking the item version and claim. |
| `outfit_board` | `outfit_board:{outfit_id}:{layout_version}` (when `outfit_id` known)<br>`outfit_board:asset:{asset_id}` (try-on / preview without outfit) | Re-renders the same outfit at the same layout return the existing image. Bumping `layout_version` forces a new render. |
| `llm_ask`      | `llm_ask:{ask_session_id}` *(planned)*    | One ask session = one job. Resending the same question reuses the existing answer. |
| `ocr_receipt`  | `ocr:{upload_id}` *(planned)*             | Each upload yields exactly one OCR run. |
| `pdf_ingest`   | `pdf:{upload_id}` *(planned)*             | Same as OCR. |
| `tryon`        | `static_tryon:{sha256(cache_key)}` | V3-017 static try-on preview. **The prefix is deliberately not the `job_type`** — the only such case in this table. Contract §6.3 freezes the key string as `static_tryon:{sha256(cache_key)}`, while `tryon` is a pre-existing *quota bucket* (`cost_guard._limit_for` maps it to `TRYON_DAILY_CALL_LIMIT`, and `GET /v1/user/cost-summary` already publishes a `tryon` field); renaming either would break something that is not V3-017's to break. The digest is over the frozen §6.3 cache key `(avatar_version_id, normalized_candidate_asset_hash, sorted_supporting_item_ids, view_type, provider_model_version, prompt_version)` — see `app/application/purchase/static_preview_cache_key.py`. Hashed from the candidate's **stable object key**, never a signed URL, for the reason `avatar_candidate` records below. Item ids are sorted and de-duplicated so the same garments in a different order are one job. The same digest is stored on `outfit_visual_assets.cache_key` under a unique index, so the enqueue race resolves in Postgres (23505 → read the winner back) rather than in a check-then-insert guard. `max_attempts=3` — a lost preview leaves a user watching a spinner. **Terminal rows are revived, not re-keyed**, the `avatar_preparation` decision applied unchanged: a `dead`/`failed` row owns the key forever, so `RequestStaticPreview._serve_cached` re-opens *that* row when the user asks again (`succeeded` is deliberately not revivable, which is what keeps "no duplicate paid jobs" true). **Has a dead-letter reconciler** (`reconcile_dead_static_tryon_assets`), unlike the avatar jobs: an asset row is user-visible and would otherwise sit on `processing` forever. Quota is admitted through the **reservation ledger** (`paid_generation_reservations`, V3-026), not through a `cost_guard` aggregate and not through Redis. The aggregate could not count a `queued` generation as paid usage, so requests arriving faster than the cron-paced drain each counted zero and were each admitted; the Redis `INCRBY` reservation is never refunded, so it would burn a call on a cache hit and on a failed render, contradicting the call-count rule this table's `cost_guard` counterpart documents. `reserve` holds one row per unit of purchase inside a `pg_advisory_xact_lock` on `(user_id, job_type, usage_day)`, in the same transaction as the asset and the job, and `usage_today("tryon")` now reads that ledger so `GET /v1/user/cost-summary` cannot report less than admission enforces. A **revive is its own purchase** (`revive_index >= 1`) and is capped at `TRYON_MAX_REVIVES` per generation — unconditionally, not behind `ENABLE_COST_GUARD` — because a dead job frees its unit (V3-017's rule) and would otherwise be re-buyable without bound. Settlement is *derived*, never pushed by the worker: `settle_terminal_reservations` joins the job's status to the asset's `generation_status` (required, because a quality rejection is a *succeeded* job) and runs both at the tail of the drain and on its own cron, so a crashed worker leaves no budget permanently held. **A quality-gate rejection is a *succeeded* job** (V3-018): the verdict is an outcome, not an error, so `rejected_by_quality_gate` rows are served from cache like `ready` ones and are never revived — a render the gate refused once is not re-bought on the next press. The gate's regeneration loop spends provider calls *inside* one attempt (at most `1 + TRYON_QUALITY_MAX_AUTO_REGENERATIONS` renders, default budget 1 per contract §6.3), and a gate that *raises* rides the technical-retry path instead, so the worst case per job is `max_attempts × (1 + regen budget)` renders — both budgets are named constants precisely so that multiplication stays visible. |
| `cutout`       | `cutout:{item_id}`                        | One optional product-shot cutout per item. Admission reserves a unit when queued; replay reuses the job and reservation. |
| `user_purge`   | `user_purge:{user_id}`                    | Account deletion runs once per user. |
| `user_export`  | `user_export:{user_id}:{date}`            | PIPL DSAR bundle. Day-keyed + producer-side in-flight check → ≤1 concurrent export per user; a fresh request is possible the next day after terminal status. Result stores the bundle `object_key`; `GET /v1/me/data-export/{job_id}` signs the download URL at read time. |
| `avatar_candidate` | `avatar_candidate:{trigger}:{assessor}:{user_id}:{sha256(object_key)[:40]}` | V3-012 person-reference detection. Keyed on the photo, so re-seeing it never re-offers it (V3_SPEC §7.3 prompt-once); the key is a digest because `source_object_key` is up to 512 chars and `idempotency_key` is 128. Hashed from the *stable object key*, never a signed URL — otherwise a re-signed URL would mint a second job for the same photo. `trigger` is part of the key because `natural_flow` and `preview_request` ask different questions: a natural flow that went quiet after a decline must not swallow a later user-initiated preview as a duplicate. `assessor` (`unavailable` \| `human_seg`) is part of it because with no detector configured every photo is recorded `pending` — unexamined — and a key that ignored the detector would make those rows permanently unassessable once segmentation is switched on. Prompt-once is unaffected: a *terminal* verdict stops detection at the row, so the second job finds the answer already recorded. `max_attempts=1` and **no dead-letter reconciler** — a dead job leaves nothing user-visible and the photo stays eligible for a later natural flow. |
| `avatar_preparation` | `avatar_preparation:{avatar_profile_id}:{sha256(source_asset_id)[:24]}:{sha256(provider:model_version:prompt_version)[:16]}` | V3-013 canonical avatar generation. Keyed on everything that determines the paid work, so re-posting `prepare` after a success is free and returns the existing job (acceptance 2: "does not duplicate paid work"). A *different* consented photo is a different key, so `replace` is never swallowed as a duplicate of the original preparation; bumping the pipeline version deliberately re-opens the work. The pipeline triple is **digested, not embedded**: a realistic vendor triple pushed the composed key to 140 chars against `idempotency_key`'s 128, so the digest fixes it at 97 regardless of what a provider is called, and the readable value rides `payload.pipeline_identity` for operators. The producer also refuses with `4091 STATE_CONFLICT` when a job for that profile is `queued`/`running` (contract §4.2) — the 409 makes a sequential duplicate legible, the key is what holds under a race. `max_attempts=3`, unlike `avatar_candidate`: a lost preparation leaves the user visibly stuck in `preparing`. **Terminal rows are revived, not re-keyed** — this is the one job type where `enqueue` returning an existing row is not the end of the story. A `dead`/`failed` row owns the key forever, so a retry would answer 200 with a job that never runs; `PrepareAvatar._revive_if_terminal` re-opens *that* row (`status=queued`, `attempts=0`, `last_error` preserved) under a conditional UPDATE, chosen over adding an attempt component to the key so one photo's cost and error history stay on one row. `succeeded` is deliberately not revivable, which is what keeps acceptance 2 intact. **No dead-letter reconciler** — a dead job's rollback to `candidate` happens on the failure path (terminal failures only; while attempts remain the profile stays `preparing`), and the user-initiated retry is the recovery path. Note the lease sweep does **not** cover this: `recover_expired_leases` dead-letters a crash-looping job directly without calling `mark_failed`, so no rollback fires and the profile stays `preparing` — also recoverable by retry, since `preparing` is prepare-able and a `dead` job is not in flight. |
| `avatar_purge` | `avatar_purge:{avatar_profile_id}:{sha256(sorted object keys)[:32]}` | V3-013 storage removal after `DELETE /v1/avatar-profile`. **The key set is in the key, and the profile id alone is not enough**: a deleted profile survives as a `disabled` tombstone and keeps its id, so delete → re-enable → prepare → delete again composed the same key twice and `ON CONFLICT DO NOTHING` returned the *succeeded* first job without replacing its payload — the rebuilt avatar's objects were never swept while the response reported them as scheduled. Digesting the sorted key set keeps retry idempotency (two DELETEs over the same objects still collapse to one sweep) while giving a genuinely new deletion its own job; sorted because the plan is assembled from two queries and an unstable order would mint a new key for an identical deletion. The DB rows are deleted in the request; this removes the objects out of band, because object deletion is neither transactional nor reversible and a rollback after a successful `delete_objects` would leave rows intact and images destroyed. Payload carries stable object keys, never signed URLs (contract §7), and `mark_failed` persists only the exception type — a storage SDK error quotes the failing key, and `async_jobs.error` outlives log retention. `max_attempts=3` and retry-safe by construction (`delete_objects` is documented idempotent): a dropped purge leaves person material in the bucket after a deletion request, which is the opposite trade-off from `avatar_candidate`. **No dead-letter reconciler** — nothing user-visible is pending, and `_alert_dead` already raises the Sentry event an operator can act on. |
| `import_task`  | `import_task:{task_id}` then `import_task:{task_id}:{generation}` | Each manual retry of a retryable failed import creates a new generation. The same task ID and committed source survive. |
| `garment_crop` | `garment_crop:{task_id}:{sha256(candidate_id + ':' + source_version)}` | One independently retryable crop per recognized garment and source. The committed recognition survives crop failure. The worker reviews each crop before exposing it. |

Keep keys ≤ 128 chars (the column limit). Use lowercase. Don't put PII into
keys — `user_id` UUIDs are fine, raw emails / phone numbers are not.

## Retry + backoff

Exponential schedule: `60s → 5min → 30min → 2h`, then capped. After
`max_attempts` failures the row goes to `dead` regardless of which path
(mark_failed or lease recovery) detected the exhaustion. Default
`max_attempts=3`; producers may override per-job.

## Lease + crash recovery

`claim_one` sets `lease_expires_at = now + DEFAULT_LEASE_SECONDS` (10 min).
`recover_expired_leases` is a periodic cron sweep (every 15 min) that:

- routes rows whose `attempts >= max_attempts` to `dead` directly
  (this closes the "crashloop never dead-letters" hole)
- returns the rest to `queued` with `attempts` preserved

Worst-case dead-detection latency for a hard-killed worker:
`lease (10m) + recovery interval (15m) = 25m`.

## Cost accounting

`cost_tokens` and `cost_cents` are atomic counters incremented by
`mark_succeeded`. Producers SHOULD pass real values from the underlying
provider's response. Embedding workers currently pass `0/0` because local
CLIP has no token cost; remote / LLM providers must populate.

Phase E (cost guardrails) consumes these via `app/application/cost_guard.py`
to enforce per-user-per-day limits.

## Correlation IDs

`CorrelationIdMiddleware` puts a UUID into `structlog.contextvars` for each
HTTP request and echoes it back in `X-Correlation-Id`. Producers SHOULD pass
the active correlation_id when enqueueing so the worker can rebind it
during processing — that's the only thread that lets you grep "what
happened to that user's request from API entry through dead-letter."

## Admin observability

`GET /v1/admin/jobs?status=dead` is the oncall view. Gated by
`require_admin_user` (settings: `ADMIN_USER_IDS`). Default fail-closed in
prod: empty `ADMIN_USER_IDS` → 403.

## Producer template

```python
from app.infrastructure.workers.job_runtime import enqueue

async def my_use_case(session, item_id, user_id, correlation_id):
    job, inserted = await enqueue(
        session,
        job_type="example",
        idempotency_key=f"example:{item_id}",
        payload={"item_id": str(item_id)},
        user_id=user_id,
        target_id=item_id,
        correlation_id=correlation_id,
    )
    # `inserted` is False when the same key already exists — that's success.
    # Caller owns commit.
```

## Consumer template

Drainer pattern (one short transaction per claim/finalize so a worker crash
between calls doesn't poison the next claim):

```python
async def drain_my_jobs(ctx):
    for _ in range(MAX_DRAIN_PER_TICK):
        async with AsyncSessionLocal() as s:
            job = await claim_one(s, ["my_job_type"])
            # claim_one commits the claim itself.
        if job is None:
            break
        try:
            structlog.contextvars.bind_contextvars(
                correlation_id=job.correlation_id, job_id=str(job.id)
            )
            result = await do_external_work(job.payload)
            async with AsyncSessionLocal() as s:
                await mark_succeeded(s, job.id, claimed_attempt=job.attempts)
                await s.commit()
        except Exception as exc:
            async with AsyncSessionLocal() as s:
                await mark_failed(s, job.id, claimed_attempt=job.attempts, error=type(exc).__name__)
                await s.commit()
        finally:
            structlog.contextvars.unbind_contextvars("correlation_id", "job_id")
```

Register the drainer via arq cron in `worker_main.py`. Lease recovery is
shared across all `job_type`s — do NOT register a per-worker recovery; one
`recover_async_job_leases` cron covers the whole table.


## Import stages

Recognition commits `processing`, then calls the provider outside the database
transaction. An external image is snapshotted before recognition and a retry
reuses the saved key. Recognition commits the candidates, source version and
crop jobs together. A crash after the provider responds but before that commit
can repeat the provider call.

A permanent rejection is a job result with `outcome=rejected` and a failed
import status. Network errors, rate limits, provider 5xx and malformed model
responses use bounded retries. Read job outcome as well as execution status
when measuring recognition success.

The import and crop drainers check their remaining arq budget before claiming.
Import uses a 60-second attempt and 75-second lease. Crop uses a 45-second
attempt and 60-second lease. Defaults leave five seconds before the outer
300-second deadline. Cutout uses up to 225 seconds per attempt with the default
provider timeouts and keeps the runtime's 10-minute lease. Each drain stops
claiming when the next attempt would exceed its remaining budget. Import and
crop sweep their expired claims every five seconds while workers are running.
The shared 15-minute sweep also covers cutout. A cancelled attempt has four
seconds to record failure and backoff before relying on lease recovery.

The same import task lock protects confirmation and crop adoption. If an item
exists when the crop finishes, the worker also locks that item and queues the
same derivative work as confirmation. Each upload attempt has its own key,
so recovery can overlap a late upload without overwriting adopted bytes.

Cutout holds the existing paid-generation reservation until the job settles.
Success and quality rejection consume the unit. Technical failure or a skip
before generation releases it. The job records an unknown provider cost as
null. The dead-cutout cron repairs abandoned reservations. Legacy queued or
running jobs without reservations count against admission until they finish.
Qualifying legacy successes count on their UTC completion day.
