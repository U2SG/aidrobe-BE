"""V3-026 — the one door a paid visual generation is admitted through.

`request_static_preview` is the only caller today. It is written to be the only
*primitive*: a future live-VTO session reserves a bounded session budget through
the same four steps while its media path stays provider-specific, which is what
aidrobe-BE#158 asks for.

```text
cache / idempotency lookup      (the caller's — a hit never reaches this module)
  -> reserve            atomic, durable, before the job can become runnable
  -> attach             the job and asset the reservation paid for
  -> settle / release   terminal accounting, or a refund for work never started
```

**WHY A LEDGER AND NOT A COUNT.** Admission used to be derived from
`async_jobs` (`cost_guard.usage_today`, call-count mode: `COUNT(*)` over
`succeeded`/`running`). Two things that count cannot express are exactly the two
gaps V3-017 recorded and this feature closes:

- `queued` is not counted and is a *persistent* state, so requests arriving
  faster than `MAX_DRAIN_PER_TICK=5` cron-paced drain each counted zero and were
  each admitted. No concurrency needed. A reservation is held from admission, so
  the bound stops depending on worker throughput.
- A revive inserts no row, so nothing derived from `async_jobs` can tell the
  first press from the fiftieth. `revive_index` makes the press countable, and
  therefore boundable.

**WHY THE ADVISORY LOCK IS THE ATOMICITY, and what is NOT inside it.** The
admission predicate is `COUNT(...) < limit` followed by an `INSERT`, which no
single statement makes safe under READ COMMITTED: two transactions would both
read 4 of 5 and both insert. `pg_advisory_xact_lock` on
`(user_id, job_type, usage_day)` serialises the pair, using the idiom
`recommendation_repo._lock_daily_write` already established for the same shape
of problem (sha1 → 8 bytes → signed int). The lock is transaction-scoped, so it
releases on commit or rollback with no unlock path to forget.

What is *not* inside that lock: any provider call. V3-017's whole request path
exists so nothing expensive happens before the worker claims the job, and this
module keeps that — the lock covers one COUNT, one SELECT and one INSERT against
indexed columns of one table. A held lock never waits on a network.

**WHAT COUNTS AGAINST THE BUDGET, and the passed criterion that decides it.**
`COUNTED_OUTCOMES` is `succeeded` + `quality_rejected` — plus every held row.
A `technical_failure` does not count, because V3-017 acceptance 3 ("failed calls
do not burn call quota according to established cost-guard rules") is a passed
criterion still in force, and its stated reason is a real one: a provider outage
must not lock a user out of the preview they are about to retry. `abandoned` does
not count because the reservation bought nothing at all.

Stated plainly, because it is the limit of this feature and not a defect it
introduces: while a provider fails every render, a user with N distinct
decisions can still spend N × MAX_ATTEMPTS provider attempts in a day, since
each dead generation refunds its unit. Closing that means deciding that failures
*do* burn budget — a spending-policy reversal of V3-017 acceptance 3, which no
V3-026 criterion asks for and which is the `spending_budget` human gate's call,
not this module's. What V3-026 does bound is the loop the issue names: the same
generation cannot be revived without end (`assert_revive_budget`), and no amount
of queueing can exceed the configured budget at any instant.

**SETTLEMENT IS DERIVED, NOT PUSHED.** The worker writes no reservation state.
`settle_terminal_reservations` reads the terminal truth that already exists —
the job's status and the asset's `generation_status` — which keeps the paid path
free of a second bookkeeping write that could be skipped on a crash. It must
join the asset, not read the job alone: V3-018 makes a quality rejection a
*succeeded* job on purpose, so `AsyncJob.status` cannot tell `succeeded` from
`quality_rejected` and acceptance 6's distinction would silently collapse into
one bucket.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, Select, and_, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import error_codes
from app.common.enums import PaidGenerationOutcome
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.paid_generation import PaidGenerationReservation
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_DEAD,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
)

logger = structlog.get_logger(__name__)

#: Outcomes that keep consuming the day's budget after settlement. Held rows
#: (outcome IS NULL) always count — see the module docstring for why the two
#: absent members are absent.
COUNTED_OUTCOMES: tuple[str, ...] = (
    PaidGenerationOutcome.SUCCEEDED.value,
    PaidGenerationOutcome.QUALITY_REJECTED.value,
)

#: The job states from which a reservation can be settled. `failed` is included
#: because `RequestStaticPreview._serve_cached` treats it as revivable, i.e. as
#: terminal from the request path's point of view; leaving it out would strand
#: such a reservation held forever and hold budget nobody can spend.
_TERMINAL_JOB_STATUSES: tuple[str, ...] = (
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
    JOB_STATUS_DEAD,
)

#: V3-018's rejected terminal state, matched to decide `quality_rejected` vs
#: `succeeded`. `static_tryon_worker.STATUS_REJECTED` holds the same value and
#: is what writes the row, but that module is a leaf worker — `worker_main` is
#: its only importer — and pulling it into the request path would drag the
#: provider adapters and storage wiring behind it. Contrast the `JOB_STATUS_*`
#: imports above: `job_runtime` is the shared runtime that eight application
#: modules already depend on, so importing from it costs nothing.
#:
#: That leaves a real duplication, so it is guarded rather than described:
#: `tests/unit/test_v3_paid_generation_admission_sql.py` asserts this equals
#: `static_tryon_worker.STATUS_REJECTED`. Without it the settlement predicate
#: could silently stop matching the worker and every quality rejection would be
#: booked as a plain success. A test may import across layers; this module
#: may not.
_ASSET_STATUS_REJECTED = "rejected_by_quality_gate"

#: `data.reason` on the 429, so FE can tell "come back tomorrow" from "this
#: render keeps failing, stop pressing". Additive on an existing status code —
#: contract §3 forbids new top-level error codes, not new `data` values.
REASON_DAILY_BUDGET = "daily_budget_exhausted"
REASON_RETRY_BUDGET = "retry_budget_exhausted"


@dataclass(frozen=True)
class Admission:
    """A granted reservation, and whether THIS call is what created it."""

    reservation_id: uuid.UUID
    created: bool


def _today() -> date:
    return datetime.now(UTC).date()


def _refuse(reason: str, message: str) -> FriendlyException:
    return FriendlyException(
        error_codes.STATE_CONFLICT,
        message,
        status_code=429,
        data={"reason": reason},
    )


def bucket_lock_key(user_id: uuid.UUID, job_type: str, usage_day: date) -> int:
    """The advisory-lock key for one (user, job_type, day) bucket.

    The idiom is `recommendation_repo._lock_daily_write`'s — sha1, first 8
    bytes, signed int. The `paid_generation:` prefix keeps this namespace
    disjoint from that one: advisory-lock keys are global to the database, so
    two features hashing the same user and date would silently serialise against
    each other.
    """
    payload = f"paid_generation:{user_id}:{job_type}:{usage_day.isoformat()}"
    digest = hashlib.sha1(payload.encode()).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


async def _lock_bucket(
    session: AsyncSession, user_id: uuid.UUID, job_type: str, usage_day: date
) -> None:
    """Serialise one bucket for the rest of this transaction."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": bucket_lock_key(user_id, job_type, usage_day)},
    )


def counted_reservations_statement(
    user_id: uuid.UUID, job_type: str, usage_day: date
) -> Select[tuple[int]]:
    """The admission predicate, as a statement so it can be read without a DB.

    Factored out for the same reason V3-005/V3-006 factored their scan windows:
    the *predicate* is the correctness claim, and asserting it on compiled SQL is
    the only evidence of it obtainable on a host that cannot run the
    testcontainers suite.
    """
    return (
        select(func.count())
        .select_from(PaidGenerationReservation)
        .where(
            PaidGenerationReservation.user_id == user_id,
            PaidGenerationReservation.job_type == job_type,
            PaidGenerationReservation.usage_day == usage_day,
            or_(
                PaidGenerationReservation.outcome.is_(None),
                PaidGenerationReservation.outcome.in_(COUNTED_OUTCOMES),
            ),
        )
    )


def revive_count_statement(target_id: uuid.UUID) -> Select[tuple[int]]:
    """Every revive ever admitted for one generation, in any outcome.

    No outcome filter, on purpose: the cap is a safety bound on how many times
    the same render may be re-attempted, so refunding a revive's budget must not
    also hand back its retry allowance.
    """
    return (
        select(func.count())
        .select_from(PaidGenerationReservation)
        .where(
            PaidGenerationReservation.target_id == target_id,
            PaidGenerationReservation.revive_index >= 1,
        )
    )


async def counted_reservations_today(
    session: AsyncSession,
    user_id: uuid.UUID,
    job_type: str,
    *,
    usage_day: date | None = None,
) -> int:
    """How much of today's budget this bucket has committed.

    Held rows count at full value: a queued generation is money about to be
    spent, and treating it as free is the over-admission this feature exists to
    close.
    """
    day = usage_day or _today()
    count = int(
        (await session.execute(counted_reservations_statement(user_id, job_type, day))).scalar()
        or 0
    )
    if job_type != "cutout":
        return count
    # Before reservation-backed admission, cutout jobs were enqueued directly.
    # Hold space for the pending backlog until it drains, and count its paid
    # completions on their UTC completion day. Linked jobs use only the ledger.
    start = datetime.combine(day, time.min, tzinfo=UTC)
    legacy_count = await session.scalar(
        select(func.count())
        .select_from(AsyncJob)
        .where(
            AsyncJob.user_id == user_id,
            AsyncJob.job_type == job_type,
            ~select(PaidGenerationReservation.id).where(PaidGenerationReservation.job_id == AsyncJob.id).exists(),
            or_(
                AsyncJob.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RUNNING)),
                and_(
                    AsyncJob.status == JOB_STATUS_SUCCEEDED,
                    AsyncJob.updated_at >= start,
                    AsyncJob.updated_at < start + timedelta(days=1),
                    AsyncJob.result["outcome"].astext.is_distinct_from("not_called"),
                ),
            ),
        )
    )
    return count + int(legacy_count or 0)


async def revive_count(session: AsyncSession, target_id: uuid.UUID) -> int:
    return int((await session.execute(revive_count_statement(target_id))).scalar() or 0)


async def assert_revive_budget(
    session: AsyncSession,
    *,
    target_id: uuid.UUID,
    job_type: str,
    max_revives: int,
) -> int:
    """Raise 429 when this generation has been revived `max_revives` times.

    Returns the `revive_index` the next revive would take (1-based).

    Unconditional — not gated by `ENABLE_COST_GUARD`, unlike the daily budget.
    The daily budget is a *spending* policy someone configures; this is the
    hard bound that stops a persistently failing provider being re-bought by
    pressing a button, and a deployment with the cost guard off must not be the
    deployment where that loop is unbounded.
    """
    used = await revive_count(session, target_id)
    if used >= max_revives:
        logger.warning(
            "paid_generation.revive_budget_exhausted",
            job_type=job_type,
            target_id=str(target_id),
            revives=used,
            max_revives=max_revives,
        )
        raise _refuse(
            REASON_RETRY_BUDGET,
            f"{job_type} retry budget exhausted for this generation",
        )
    return used + 1


async def reserve(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    job_type: str,
    reservation_key: str,
    attempt_grant: int,
    limit: int | None,
    revive_index: int = 0,
    target_id: uuid.UUID | None = None,
) -> Admission:
    """Hold one unit of today's budget, or raise 429.

    `limit=None` means no budget is in force (`ENABLE_COST_GUARD` off, or the
    bucket has no configured cap). The row is still written: the ledger is the
    durable accounting record and the revive cap reads it, so it must exist
    whether or not anyone is enforcing a daily number today.

    Idempotent on `(reservation_key, usage_day)`. A replay of the same purchase
    returns the existing reservation with `created=False` and consumes nothing —
    which is also what a duplicate concurrent request sees once the lock
    releases, and the reason `created` must be checked before `release`:
    releasing a reservation this call did not create would refund the request
    that won.

    **The day is part of the key**, because the budget is daily and the unit of
    purchase is therefore "this generation, on this day". Keyed on the string
    alone, the ledger could hold at most one row per generation *ever*, so a
    later day's request for the same key would replay a settled row and consume
    nothing — which makes `usage_day` decorative for that key rather than the
    thing the budget is drawn against.

    Whether that replay can buy an actual provider call today is narrower, and
    worth stating rather than overselling: the same key is reachable again only
    when the asset is gone (V3-011's retention sweep drops derived renders on a
    clock the avatar version outlives), and the enqueue would then still be
    absorbed by `job_runtime.enqueue`'s own lifetime idempotency on the same key
    — so nothing is rendered and nothing is spent. It becomes a free paid render
    the moment anything reaps old `async_jobs` rows, which nothing does yet. The
    day is in the key because daily uniqueness is what a daily budget means, not
    because a live exploit was found.
    """
    day = _today()
    await _lock_bucket(session, user_id, job_type, day)

    existing = await session.scalar(
        select(PaidGenerationReservation).where(
            PaidGenerationReservation.reservation_key == reservation_key,
            PaidGenerationReservation.usage_day == day,
        )
    )
    if existing is not None:
        logger.info(
            "paid_generation.reservation_replay",
            job_type=job_type,
            reservation_id=str(existing.id),
        )
        return Admission(reservation_id=existing.id, created=False)

    if limit is not None:
        used = await counted_reservations_today(session, user_id, job_type, usage_day=day)
        if used >= limit:
            logger.warning(
                "paid_generation.budget_exhausted",
                job_type=job_type,
                used=used,
                limit=limit,
                revive_index=revive_index,
            )
            raise _refuse(REASON_DAILY_BUDGET, f"daily {job_type} quota exhausted")

    reservation = PaidGenerationReservation(
        user_id=user_id,
        job_type=job_type,
        usage_day=day,
        reservation_key=reservation_key,
        revive_index=revive_index,
        attempt_grant=attempt_grant,
        target_id=target_id,
    )
    session.add(reservation)
    # No `ON CONFLICT DO NOTHING` here, deliberately. Inside the bucket lock a
    # duplicate key cannot be a race, so a 23505 would mean the lock namespace
    # no longer covers the key — a bug that must surface rather than be
    # absorbed into a silent read-back.
    await session.flush()
    logger.info(
        "paid_generation.reserved",
        job_type=job_type,
        reservation_id=str(reservation.id),
        revive_index=revive_index,
        attempt_grant=attempt_grant,
    )
    return Admission(reservation_id=reservation.id, created=True)


async def attach(
    session: AsyncSession,
    reservation_id: uuid.UUID,
    *,
    job_id: uuid.UUID,
    target_id: uuid.UUID | None,
) -> None:
    """Point a held reservation at the job and asset it paid for.

    Separate from `reserve` because the asset row does not exist yet when the
    budget is taken — reserving first is what makes "reserved before the job can
    become runnable" true in statement order and not merely in commit order.
    Both writes share the caller's transaction, so a failure anywhere between
    them rolls the reservation back with the work it was for.
    """
    await session.execute(
        update(PaidGenerationReservation)
        .where(PaidGenerationReservation.id == reservation_id)
        .values(job_id=job_id, target_id=target_id)
    )


async def release(
    session: AsyncSession,
    reservation_id: uuid.UUID,
    *,
    reason: str,
) -> None:
    """Settle a reservation that bought nothing, refunding its unit.

    Reachable from exactly the paths where admission succeeded and the work then
    did not start: the enqueue lost its insert race (another request had already
    created this preview) and a revive lost its conditional UPDATE (another
    retry re-opened the job first). Both leave a granted unit that no provider
    call will ever consume.
    """
    await session.execute(
        update(PaidGenerationReservation)
        .where(
            PaidGenerationReservation.id == reservation_id,
            PaidGenerationReservation.outcome.is_(None),
        )
        .values(
            outcome=PaidGenerationOutcome.ABANDONED.value,
            attempts_used=0,
            settled_at=datetime.now(UTC),
        )
    )
    logger.info("paid_generation.released", reservation_id=str(reservation_id), reason=reason)


def _regenerations(quality_signals: dict[str, Any] | None) -> int:
    """V3-018's `render_count` minus the first render, or 0 when nothing rendered."""
    render_count = (quality_signals or {}).get("render_count")
    if not isinstance(render_count, int) or render_count < 1:
        return 0
    return render_count - 1


async def settle_terminal_reservations(
    session: AsyncSession,
    *,
    job_type: str,
    target_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
) -> int:
    """Settle every held reservation whose job has reached a terminal state.

    This is acceptance 7's convergence, and it is idempotent by construction:
    the WHERE clause only ever matches `outcome IS NULL`, so running it twice
    settles nothing twice and a crash mid-sweep is resumed by the next tick.

    A reservation whose job dead-lettered settles as `technical_failure`, which
    stops counting against the budget — that is V3-017 acceptance 3's rule, and
    it is the reason a crashed worker cannot leave budget permanently held.

    `target_id` narrows the sweep to one generation. The revive path passes it
    for two reasons that are both correctness, not tidiness:

    1. **Outcome accuracy.** A revive re-opens the job, so a reservation still
       held when that happens would later be settled from the *new* attempt's
       status and a generation that failed would be recorded as `succeeded`.
       Closing the books first is the only point at which "this attempt-set
       ended technically" is still true.
    2. **The user can actually retry.** A dead generation's unit is refunded at
       settlement; if that had not happened yet, a user whose only generation
       died would be refused a retry by budget its own dead predecessor was
       holding — until a cron ran. Which is a 429 the user cannot act on, the
       exact failure V3-017 acceptance 3 exists to prevent.

    Caller owns the commit.
    """
    rows = (
        await session.execute(
            select(
                PaidGenerationReservation.id,
                AsyncJob.status,
                AsyncJob.attempts,
                AsyncJob.result,
                OutfitVisualAsset.generation_status,
                OutfitVisualAsset.quality_signals,
            )
            .join(AsyncJob, AsyncJob.id == PaidGenerationReservation.job_id)
            .outerjoin(
                OutfitVisualAsset,
                OutfitVisualAsset.id == PaidGenerationReservation.target_id,
            )
            .where(
                PaidGenerationReservation.outcome.is_(None),
                PaidGenerationReservation.job_type == job_type,
                AsyncJob.status.in_(_TERMINAL_JOB_STATUSES),
                *(
                    [PaidGenerationReservation.target_id == target_id]
                    if target_id is not None
                    else []
                ),
                *([AsyncJob.id == job_id] if job_id is not None else []),
            )
        )
    ).all()
    if not rows:
        return 0

    now = datetime.now(UTC)
    settled = 0
    for reservation_id, job_status, attempts, job_result, asset_status, quality_signals in rows:
        if job_status != JOB_STATUS_SUCCEEDED:
            outcome = PaidGenerationOutcome.TECHNICAL_FAILURE
        elif job_type == "cutout":
            outcome = {
                "not_called": PaidGenerationOutcome.ABANDONED,
                "quality_rejected": PaidGenerationOutcome.QUALITY_REJECTED,
            }.get((job_result or {}).get("outcome"), PaidGenerationOutcome.SUCCEEDED)
        else:
            # The join is what makes this distinguishable at all: V3-018 marks a
            # quality rejection as a SUCCEEDED job, so the job row alone would
            # report both as `succeeded`.
            outcome = (
                PaidGenerationOutcome.QUALITY_REJECTED
                if asset_status == _ASSET_STATUS_REJECTED
                else PaidGenerationOutcome.SUCCEEDED
            )
        result = cast(
            CursorResult[Any],
            await session.execute(
                update(PaidGenerationReservation)
                .where(
                    PaidGenerationReservation.id == reservation_id,
                    PaidGenerationReservation.outcome.is_(None),
                )
                .values(
                    outcome=outcome.value,
                    attempts_used=0 if outcome == PaidGenerationOutcome.ABANDONED and attempts == 1 else attempts,
                    quality_regenerations=_regenerations(quality_signals),
                    settled_at=now,
                )
            ),
        )
        settled += result.rowcount or 0
    if settled:
        logger.info("paid_generation.settled", job_type=job_type, count=settled)
    return settled
