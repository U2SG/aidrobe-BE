"""V3-013 — run avatar preparation out of band.

Consumes `async_jobs` rows of type `avatar_preparation` enqueued by
`app.application.avatar.prepare_avatar`. The request that asked for a
preparation never calls a provider: contract §4.2's "start preparation" is an
enqueue, and everything expensive happens here on its own `AsyncSessionLocal`
session, the same shape as `avatar_candidate_worker`.

**The success path is the atomic swap.** A new `AvatarVersion` row is written and
then `AvatarLifecycle.activate_version` retires the previous one and moves the
pointer in the *same* transaction. Nothing here mutates an existing version, so
`replace` cannot damage a working avatar even mid-flight: until this commits, the
old version is still the active one, and a crash before the commit leaves the
user exactly where they were.

**The failure path is deliberately asymmetric.** A first-time preparation rolls
the profile back to `candidate` (contract §4.1) so the user can retry; a
`replace` failure does nothing at all, because the profile is still `ready` on
its previous version and that is precisely the guarantee — see
`AvatarLifecycle.fail_preparation`. The rollback runs on its own session *after*
the failure, so it survives the rollback of the transaction that failed.

**Ordering: storage first, rows second.** The canonical renders are uploaded
before the version row is written. The reverse order can produce a `ready`
profile pointing at an object that does not exist — a broken avatar the user
cannot fix — whereas this order's worst case is an orphaned object with no row,
which costs storage and is invisible to the user. Given a choice between leaking
bytes and showing a user a broken likeness of themselves, leak the bytes.

**What CI exercises.** `FakeAvatarPreparation` is local, deterministic and the
only adapter in the tree (contract §6.4), so the whole `preparing → ready`
transition runs for real in tests — no container, no network, no spend. What is
*not* proven anywhere: that a production model produces a usable canonical
avatar from a real photograph. That is the budgeted, human-gated provider
evaluation, and the same honest limit V3-012 recorded for its detector.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from app.application.ports.avatar_preparation import AvatarPreparationPort
from app.bootstrap.container import avatar_preparation, object_storage
from app.infrastructure.persistence.models.avatar import AvatarProfile, AvatarVersion
from app.infrastructure.persistence.repositories.avatar_repo import AvatarLifecycle
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.workers.job_runtime import (
    claim_one,
    mark_failed,
    mark_succeeded,
)

logger = structlog.get_logger(__name__)

JOB_TYPE = "avatar_preparation"
MAX_DRAIN_PER_TICK = 5

#: Canonical renders live beside the user's other objects, under the prefix
#: `POST /v1/uploads/presign` already issues, so `is_owned_object_key` recognizes
#: them and the account-level purge's `delete_prefix("users/<id>/")` catch-all
#: sweeps them even if a row is gone (Phase E1 precedent).
_CANONICAL_PREFIX = "avatar/canonical"


def _canonical_key(user_id: uuid.UUID, version_id: uuid.UUID, view: str) -> str:
    return f"users/{user_id}/{_CANONICAL_PREFIX}/{version_id}_{view}.png"


async def _process_one(job: Any, *, preparation: AvatarPreparationPort | None = None) -> None:
    payload: dict[str, Any] = job.payload or {}
    structlog.contextvars.bind_contextvars(
        correlation_id=job.correlation_id or "",
        job_id=str(job.id),
        user_id=payload.get("user_id", ""),
    )
    try:
        try:
            user_id = uuid.UUID(payload["user_id"])
            profile_id = uuid.UUID(payload["avatar_profile_id"])
            source_asset_id = uuid.UUID(payload["source_asset_id"])
            source_object_key = payload["source_object_key"]
            if not source_object_key:
                raise ValueError("missing source_object_key")
        except (KeyError, ValueError, TypeError):
            # Permanently bad payload. A fixed string, never the exception text:
            # `uuid.UUID("…")` quotes the offending value back and
            # `async_jobs.error` is a persisted column that outlives log
            # retention — the V3-012 round-3 lesson, and this job's payload
            # carries an object key.
            async with AsyncSessionLocal() as session:
                await mark_failed(
                    session, job.id, claimed_attempt=job.attempts, error="invalid_payload"
                )
                await session.commit()
            return

        try:
            port = preparation if preparation is not None else avatar_preparation()
            storage = object_storage()

            source_image = await storage.get_object(source_object_key)
            prepared = await port.prepare(source_image=source_image)

            version_id = uuid.uuid4()
            front_key = _canonical_key(user_id, version_id, "front")
            await storage.put_object(front_key, prepared.front_image, "image/png")
            side_key: str | None = None
            if prepared.side_image is not None:
                side_key = _canonical_key(user_id, version_id, "side")
                await storage.put_object(side_key, prepared.side_image, "image/png")

            async with AsyncSessionLocal() as session:
                profile = await session.get(AvatarProfile, profile_id)
                if profile is None or profile.user_id != user_id:
                    # The profile was deleted (or never belonged to this user)
                    # while the provider was working. Nothing to activate; the
                    # freshly written objects are orphaned and swept by the
                    # account-level prefix purge. Not an error — the user's
                    # deletion is allowed to win a race with their own
                    # preparation.
                    await mark_succeeded(session, job.id, claimed_attempt=job.attempts)
                    await session.commit()
                    logger.info("avatar.preparation.profile_gone_discarding")
                    return

                version = AvatarVersion(
                    id=version_id,
                    avatar_profile_id=profile.id,
                    source_asset_ids=[str(source_asset_id)],
                    provider=prepared.provider,
                    model_version=prepared.model_version,
                    prompt_version=prepared.prompt_version,
                    canonical_front_object_key=front_key,
                    canonical_side_object_key=side_key,
                )
                session.add(version)
                await session.flush()
                await AvatarLifecycle(session).activate_version(profile, version)

                ok = await mark_succeeded(session, job.id, claimed_attempt=job.attempts)
                if not ok:
                    # Lost the lease: another worker owns this job now. Roll the
                    # whole transaction back rather than activating a version
                    # twice.
                    await session.rollback()
                    logger.warning("avatar.preparation.lease_lost_discarding")
                    return
                await session.commit()

            # Provenance and status only — no keys, no URLs, no person detail
            # (contract §7). `provider` / `model_version` are internal columns and
            # this is an internal log, not a response (contract §6.4).
            logger.info(
                "avatar.preparation.completed",
                provider=prepared.provider,
                model_version=prepared.model_version,
                has_side_view=side_key is not None,
            )

        except Exception as exc:
            logger.exception("avatar.preparation.failed", error_type=type(exc).__name__)
            async with AsyncSessionLocal() as session:
                await mark_failed(
                    session, job.id, claimed_attempt=job.attempts, error=type(exc).__name__
                )
                await session.commit()
            # Rolled back on a separate session, after the failure, so it is not
            # lost with the transaction that failed. A `replace` failure is a
            # no-op here by design (see `fail_preparation`).
            async with AsyncSessionLocal() as session:
                profile = await session.get(AvatarProfile, profile_id)
                if profile is not None and profile.user_id == user_id:
                    moved = await AvatarLifecycle(session).fail_preparation(profile)
                    await session.commit()
                    logger.info("avatar.preparation.rolled_back", moved=moved)
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id", "job_id", "user_id")


async def drain_avatar_preparation_jobs(ctx: object) -> int:
    processed = 0
    for _ in range(MAX_DRAIN_PER_TICK):
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [JOB_TYPE])
        if job is None:
            break
        await _process_one(job)
        processed += 1
    if processed:
        logger.info("avatar.preparation.drain_completed", processed=processed)
    return processed
