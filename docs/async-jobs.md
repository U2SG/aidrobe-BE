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
- `mark_*` are guarded by `WHERE status='running'` so stale finalizers
  cannot resurrect terminal jobs.
- `dead` is terminal. Sentry pages on every transition into `dead`
  (both `mark_failed` and lease-recovery paths).

## Idempotency key conventions

Producers MUST construct keys so re-enqueueing the same logical work returns
the existing job rather than creating a second one.

| `job_type`     | key format                                | rationale |
|----------------|-------------------------------------------|-----------|
| `embedding`    | `embedding:{item_id}:{model_version}`     | Re-uploads / image replacement of the same item shouldn't double-charge CLIP. Different `model_version` → fresh job (intentional, swap models). |
| `outfit_board` | `outfit_board:{outfit_id}:{layout_version}` (when `outfit_id` known)<br>`outfit_board:asset:{asset_id}` (try-on / preview without outfit) | Re-renders the same outfit at the same layout return the existing image. Bumping `layout_version` forces a new render. |
| `llm_ask`      | `llm_ask:{ask_session_id}` *(planned)*    | One ask session = one job. Resending the same question reuses the existing answer. |
| `ocr_receipt`  | `ocr:{upload_id}` *(planned)*             | Each upload yields exactly one OCR run. |
| `pdf_ingest`   | `pdf:{upload_id}` *(planned)*             | Same as OCR. |
| `tryon`        | `tryon:{user_id}:{outfit_id}:{body_hash}` *(planned)* | Re-running with the same body image + outfit returns existing. |
| `cutout`       | `cutout:{item_id}`                        | One product-shot cutout per item. Re-confirming the same item reuses the existing job (no double gpt-image-2 spend). |
| `user_purge`   | `user_purge:{user_id}`                    | Account deletion runs once per user. |
| `user_export`  | `user_export:{user_id}:{date}`            | PIPL DSAR bundle. Day-keyed + producer-side in-flight check → ≤1 concurrent export per user; a fresh request is possible the next day after terminal status. Result stores the bundle `object_key`; `GET /v1/me/data-export/{job_id}` signs the download URL at read time. |
| `avatar_candidate` | `avatar_candidate:{trigger}:{assessor}:{user_id}:{sha256(object_key)[:40]}` | V3-012 person-reference detection. Keyed on the photo, so re-seeing it never re-offers it (V3_SPEC §7.3 prompt-once); the key is a digest because `source_object_key` is up to 512 chars and `idempotency_key` is 128. Hashed from the *stable object key*, never a signed URL — otherwise a re-signed URL would mint a second job for the same photo. `trigger` is part of the key because `natural_flow` and `preview_request` ask different questions: a natural flow that went quiet after a decline must not swallow a later user-initiated preview as a duplicate. `assessor` (`unavailable` \| `human_seg`) is part of it because with no detector configured every photo is recorded `pending` — unexamined — and a key that ignored the detector would make those rows permanently unassessable once segmentation is switched on. Prompt-once is unaffected: a *terminal* verdict stops detection at the row, so the second job finds the answer already recorded. `max_attempts=1` and **no dead-letter reconciler** — a dead job leaves nothing user-visible and the photo stays eligible for a later natural flow. |
| `avatar_preparation` | `avatar_preparation:{avatar_profile_id}:{sha256(source_asset_id)[:24]}:{sha256(provider:model_version:prompt_version)[:16]}` | V3-013 canonical avatar generation. Keyed on everything that determines the paid work, so re-posting `prepare` after a success is free and returns the existing job (acceptance 2: "does not duplicate paid work"). A *different* consented photo is a different key, so `replace` is never swallowed as a duplicate of the original preparation; bumping the pipeline version deliberately re-opens the work. The pipeline triple is **digested, not embedded**: a realistic vendor triple pushed the composed key to 140 chars against `idempotency_key`'s 128, so the digest fixes it at 97 regardless of what a provider is called, and the readable value rides `payload.pipeline_identity` for operators. The producer also refuses with `4091 STATE_CONFLICT` when a job for that profile is `queued`/`running` (contract §4.2) — the 409 makes a sequential duplicate legible, the key is what holds under a race. `max_attempts=3`, unlike `avatar_candidate`: a lost preparation leaves the user visibly stuck in `preparing`. **Terminal rows are revived, not re-keyed** — this is the one job type where `enqueue` returning an existing row is not the end of the story. A `dead`/`failed` row owns the key forever, so a retry would answer 200 with a job that never runs; `PrepareAvatar._revive_if_terminal` re-opens *that* row (`status=queued`, `attempts=0`, `last_error` preserved) under a conditional UPDATE, chosen over adding an attempt component to the key so one photo's cost and error history stay on one row. `succeeded` is deliberately not revivable, which is what keeps acceptance 2 intact. **No dead-letter reconciler** — a dead job's rollback to `candidate` happens on the failure path (terminal failures only; while attempts remain the profile stays `preparing`), and the user-initiated retry is the recovery path. Note the lease sweep does **not** cover this: `recover_expired_leases` dead-letters a crash-looping job directly without calling `mark_failed`, so no rollback fires and the profile stays `preparing` — also recoverable by retry, since `preparing` is prepare-able and a `dead` job is not in flight. |
| `avatar_purge` | `avatar_purge:{avatar_profile_id}`         | V3-013 storage removal after `DELETE /v1/avatar-profile`. The DB rows are deleted in the request; this removes the objects out of band, because object deletion is neither transactional nor reversible and a rollback after a successful `delete_objects` would leave rows intact and images destroyed. Payload carries stable object keys, never signed URLs (contract §7), and `mark_failed` persists only the exception type — a storage SDK error quotes the failing key, and `async_jobs.error` outlives log retention. `max_attempts=3` and retry-safe by construction (`delete_objects` is documented idempotent): a dropped purge leaves person material in the bucket after a deletion request, which is the opposite trade-off from `avatar_candidate`. **No dead-letter reconciler** — nothing user-visible is pending, and `_alert_dead` already raises the Sentry event an operator can act on. |
| `import_task`  | `import_task:{task_id}`                   | One processing run per import task (Vision/OCR/link-parse). Gated by `ENABLE_IMPORT_ASYNC_JOBS`; legacy path is FastAPI BackgroundTasks. Job status = execution only; `import_tasks.task_status` stays the business source of truth. Dead-letter → `reconcile_dead_import_tasks` flips the task to `failed`. |

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
        job_type="embedding",
        idempotency_key=f"embedding:{item_id}:{model_version}",
        payload={"item_id": str(item_id), "model_version": model_version, ...},
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
                await mark_succeeded(s, job.id, cost_tokens=..., cost_cents=...)
                await s.commit()
        except Exception as exc:
            async with AsyncSessionLocal() as s:
                await mark_failed(s, job.id, error=str(exc))
                await s.commit()
        finally:
            structlog.contextvars.unbind_contextvars("correlation_id", "job_id")
```

Register the drainer via arq cron in `worker_main.py`. Lease recovery is
shared across all `job_type`s — do NOT register a per-worker recovery; one
`recover_async_job_leases` cron covers the whole table.
