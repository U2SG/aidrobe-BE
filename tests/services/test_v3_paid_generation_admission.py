"""V3-026 — paid try-on admission is atomic, bounded and reconcilable.

The four `evidence_required` items map onto the sections below:

| evidence                       | section                                   |
|--------------------------------|-------------------------------------------|
| over-admission test            | "the budget cannot be over-admitted"      |
| bounded-retry and revive test  | "retries and revives are bounded"         |
| reconciliation test            | "reservations converge"                   |
| flag-off zero-reservation test | "the flag off reserves nothing"           |

**Determinism, which acceptance 9 asks for explicitly** (this repository already
carries one quarantined flaky, #100). Nothing here sleeps and nothing asserts on
elapsed time:

- The sequential over-admission case needs no concurrency at all — that IS the
  reported defect, so it is expressed as ordered requests with the worker never
  drained.
- The concurrent case uses `asyncio.gather` as the barrier and asserts an
  *invariant* rather than an interleaving: out of N simultaneous requests for
  distinct previews under a budget of L, exactly L are admitted. That holds
  under every possible order, so there is nothing for the scheduler to flake on.
  Each task owns its own session, so one task blocking on the bucket's advisory
  lock cannot starve another's connection.
- Two tests force a losing branch with a patched method instead of a race, and
  each says so at the top of its docstring. The reason is stated rather than
  hidden: with the bucket lock in place the *natural* interleaving that produces
  those branches is not something a barrier can pin down, and a test that only
  sometimes exercises the branch it names is worse than one that is honest about
  being an instrument.
- Every "another day" date is written explicitly. No arithmetic on `now()`.

The seeding harness is imported from `test_v3_static_preview_jobs.py` rather than
copied — the repo's `tests.api.test_recommendations_today` precedent. A second
copy of ~200 lines of avatar/decision/candidate seeding is a second thing to
drift, and these tests are about admission, not about how a preview-ready user
gets built.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.cost_guard import usage_today
from app.application.paid_generation_admission import (
    counted_reservations_today,
    reserve,
    settle_terminal_reservations,
)
from app.application.purchase.request_static_preview import (
    JOB_TYPE,
    MAX_ATTEMPTS,
    PreviewRefusal,
    PreviewRefused,
    RequestStaticPreview,
)
from app.common.enums import PaidGenerationOutcome
from app.common.exceptions import FriendlyException
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.avatar import AvatarProfile
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.paid_generation import PaidGenerationReservation
from app.infrastructure.persistence.models.purchase_decision import WardrobeDecision
from app.infrastructure.persistence.models.user import User
from app.infrastructure.vision.fake_static_tryon import FakeStaticTryOn
from app.infrastructure.workers import static_tryon_worker
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_DEAD,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    recover_expired_leases,
)
from app.infrastructure.workers.static_tryon_worker import (
    drain_static_tryon_jobs,
    reconcile_static_tryon_reservations,
)
from tests.services.test_v3_static_preview_jobs import (
    _drain_until_dead,
    _ExplodingTryOn,
    _FakeStorage,
    _Fixture,
    _jobs,
    _only_job,
    _request,
    _seed_preview_ready_user,
    _use_quota,
)

REASON_DAILY = "daily_budget_exhausted"
REASON_RETRY = "retry_budget_exhausted"


# --- harness ---------------------------------------------------------------
#
# The three fixtures are declared here rather than imported with the helpers
# above: a pytest fixture imported into another module collides with the test
# parameter of the same name under ruff's F811, and 48 suppression comments would
# be a worse trade than these fifteen lines. The bodies are the sibling's, and the
# `AsyncSessionLocal` patch is the load-bearing part — the worker (and therefore
# the reconcile sweep the drain calls) runs on its own engine, so without it the
# fixture rows this test commits would be invisible to the code under test.


@pytest_asyncio.fixture
async def factory(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(postgres_url, future=True)
    maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(static_tryon_worker, "AsyncSessionLocal", maker)
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> _FakeStorage:
    fake = _FakeStorage()
    monkeypatch.setattr(static_tryon_worker, "object_storage", lambda: fake)
    return fake


@pytest.fixture
def renderer(monkeypatch: pytest.MonkeyPatch) -> FakeStaticTryOn:
    """The real P0 adapter, patched over the container lookup the worker does."""
    port = FakeStaticTryOn()
    monkeypatch.setattr(static_tryon_worker, "static_tryon", lambda: port)
    return port


async def _reservations(
    maker: async_sessionmaker[AsyncSession],
) -> list[PaidGenerationReservation]:
    async with maker() as session:
        return list(
            (
                await session.scalars(
                    sa.select(PaidGenerationReservation).order_by(
                        PaidGenerationReservation.revive_index,
                        PaidGenerationReservation.created_at,
                    )
                )
            ).all()
        )


async def _outcomes(maker: async_sessionmaker[AsyncSession]) -> list[str | None]:
    return [row.outcome for row in await _reservations(maker)]


async def _assets(maker: async_sessionmaker[AsyncSession]) -> list[OutfitVisualAsset]:
    async with maker() as session:
        return list((await session.scalars(sa.select(OutfitVisualAsset))).all())


async def _counted(maker: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> int:
    async with maker() as session:
        return await counted_reservations_today(session, user_id, JOB_TYPE)


async def _another_decision_for(
    maker: async_sessionmaker[AsyncSession], owner: _Fixture
) -> _Fixture:
    """A second, distinct preview for the SAME user.

    Distinct on purpose: two requests for the *same* preview collide on the cache
    key and are answered from cache, so they could never demonstrate anything
    about a budget. Sharing the owner is what puts them in one bucket.
    """
    other = await _seed_preview_ready_user(maker)
    async with maker() as session:
        await session.execute(
            sa.update(WardrobeDecision)
            .where(WardrobeDecision.id == other.decision_id)
            .values(user_id=owner.user_id)
        )
        await session.commit()
    other.user_id = owner.user_id
    return other


def _revive_cap(limit: int) -> Settings:
    return get_settings().model_copy(
        update={
            "ENABLE_V3_STATIC_TRYON": True,
            "ENABLE_V3_AVATAR": True,
            "TRYON_MAX_REVIVES": limit,
        }
    )


def _explodes(monkeypatch: pytest.MonkeyPatch) -> _ExplodingTryOn:
    exploding = _ExplodingTryOn()
    monkeypatch.setattr(
        "app.infrastructure.workers.static_tryon_worker.static_tryon", lambda: exploding
    )
    return exploding


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user = User(apple_subject=f"sub-{uuid.uuid4()}")
    session.add(user)
    await session.flush()
    return user.id


# --- the budget cannot be over-admitted ------------------------------------


async def test_rapid_sequential_requests_cannot_outrun_the_drain(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported defect, reproduced as the bound it now has.

    Three distinct previews, a budget of two, and the worker NEVER drained — so
    every job stays `queued`. Under the old DB aggregate (`COUNT` over
    `succeeded`/`running`) all three were admitted, because `queued` counted zero
    and is a *persistent* state rather than a momentary one. No concurrency was
    ever required to reach it, which is why this test has none.
    """
    _use_quota(monkeypatch, limit=2)
    first = await _seed_preview_ready_user(factory)
    second = await _another_decision_for(factory, first)
    third = await _another_decision_for(factory, first)

    await _request(factory, first)
    await _request(factory, second)
    with pytest.raises(FriendlyException) as refused:
        await _request(factory, third)

    assert refused.value.status_code == 429
    assert refused.value.data == {"reason": REASON_DAILY}
    # Two admitted, both still queued — the state the old aggregate could not see.
    jobs = await _jobs(factory)
    assert len(jobs) == 2
    assert {job.status for job in jobs} == {JOB_STATUS_QUEUED}
    assert await _outcomes(factory) == [None, None], "held, because the work has not run"


async def test_a_refused_request_writes_nothing_at_all(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusing is free: no asset the user watches spin, no job, no reservation.

    The order is what makes this true — the budget is taken before the asset row
    exists, so a refusal cannot leave a queued generation behind.
    """
    _use_quota(monkeypatch, limit=1)
    first = await _seed_preview_ready_user(factory)
    second = await _another_decision_for(factory, first)
    await _request(factory, first)

    with pytest.raises(FriendlyException):
        await _request(factory, second)

    assert len(await _assets(factory)) == 1
    assert len(await _jobs(factory)) == 1
    assert len(await _reservations(factory)) == 1


async def test_concurrent_requests_admit_exactly_the_budget(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 3's concurrent half, asserted as an invariant.

    Four simultaneous requests for four distinct previews, budget of two. The
    assertion is "exactly two admitted, exactly two refused" — true under every
    interleaving, so there is no ordering for the scheduler to flake on and no
    sleep anywhere. `gather` is the barrier.

    The advisory lock is what makes it hold: without it every request could read
    the same `count=0` and all four would be let through, which is the TOCTOU the
    DB-aggregate path documented as acceptable and this feature does not.
    """
    _use_quota(monkeypatch, limit=2)
    owner = await _seed_preview_ready_user(factory)
    fixtures = [owner, *[await _another_decision_for(factory, owner) for _ in range(3)]]

    async def _attempt(fixture: _Fixture) -> str:
        try:
            await _request(factory, fixture)
        except FriendlyException as refused:
            return f"refused:{(refused.data or {}).get('reason')}"
        return "admitted"

    results = await asyncio.gather(*(_attempt(fixture) for fixture in fixtures))

    assert sorted(results) == [
        "admitted",
        "admitted",
        f"refused:{REASON_DAILY}",
        f"refused:{REASON_DAILY}",
    ]
    assert len(await _reservations(factory)) == 2
    assert len(await _jobs(factory)) == 2


async def test_a_cache_hit_consumes_no_reservation(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    renderer: FakeStaticTryOn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 2, and the ordering `request_static_preview` calls load-bearing.

    The budget is spent by the first request and the render succeeds; asking
    again for the SAME preview must answer from cache without taking a second
    unit — otherwise hitting the daily cap would stop a user seeing the preview
    they already paid for, which spends nothing and shows nothing.
    """
    _use_quota(monkeypatch, limit=1)
    fixture = await _seed_preview_ready_user(factory)
    first = await _request(factory, fixture)
    await drain_static_tryon_jobs(ctx=None)

    again = await _request(factory, fixture)

    assert again.asset_id == first.asset_id
    assert again.created is False
    reservations = await _reservations(factory)
    assert len(reservations) == 1
    assert reservations[0].outcome == PaidGenerationOutcome.SUCCEEDED.value
    assert reservations[0].revive_index == 0


async def test_the_ledger_is_what_the_cost_summary_reports(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GET /v1/user/cost-summary` must not report less than admission enforces.

    A queued generation is spent budget, and the old aggregate could not see it —
    so a user refused with 429 would have been looking at `used: 0`.
    """
    _use_quota(monkeypatch, limit=2)
    fixture = await _seed_preview_ready_user(factory)
    await _request(factory, fixture)

    async with factory() as session:
        assert await usage_today(session, fixture.user_id, JOB_TYPE) == 1


async def test_another_days_spend_does_not_bind_today(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget is per-day, and the day is a stored column rather than a
    derivation over `created_at` — so this is asserted against an explicit date
    rather than arithmetic on `now()`."""
    _use_quota(monkeypatch, limit=1)
    fixture = await _seed_preview_ready_user(factory)
    async with factory() as session:
        session.add(
            PaidGenerationReservation(
                user_id=fixture.user_id,
                job_type=JOB_TYPE,
                usage_day=date(2020, 1, 1),
                reservation_key=f"{JOB_TYPE}:ancient:{uuid.uuid4()}",
                attempt_grant=MAX_ATTEMPTS,
            )
        )
        await session.commit()

    admitted = await _request(factory, fixture)

    assert admitted.created is True
    assert await _counted(factory, fixture.user_id) == 1, "yesterday's row is not today's spend"


# --- retries and revives are bounded --------------------------------------


async def test_one_admitted_generation_buys_a_bounded_number_of_attempts(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 4: the retries inside one reservation are bounded, and the
    bound is recorded on the reservation rather than only living in a constant.

    One reservation, `MAX_ATTEMPTS` attempts against a provider that is down,
    then dead — and no second reservation, because a technical retry re-runs the
    *same* job instead of buying a new one.
    """
    exploding = _explodes(monkeypatch)
    fixture = await _seed_preview_ready_user(factory)
    await _request(factory, fixture)
    dead = await _drain_until_dead(factory, (await _only_job(factory)).id)

    assert dead.attempts == MAX_ATTEMPTS
    assert exploding.calls == MAX_ATTEMPTS, "one provider call per attempt, no more"
    reservations = await _reservations(factory)
    assert len(reservations) == 1
    assert reservations[0].attempt_grant == MAX_ATTEMPTS
    assert reservations[0].attempts_used == MAX_ATTEMPTS
    assert reservations[0].outcome == PaidGenerationOutcome.TECHNICAL_FAILURE.value


async def test_a_persistently_failing_render_cannot_be_revived_without_end(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 5, and the defect V3-017 recorded as unowned.

    Every revive re-opens the job for another `MAX_ATTEMPTS` provider calls, and
    a dead job consumes no daily budget (V3-017 acceptance 3), so before this
    feature "press preview again" was an unbounded paid loop. With
    `TRYON_MAX_REVIVES=2` the third press is refused — and refused with a reason
    naming which bound was hit, not a generic quota message the user cannot act
    on.
    """
    exploding = _explodes(monkeypatch)
    settings = _revive_cap(2)
    fixture = await _seed_preview_ready_user(factory)
    await _request(factory, fixture, settings=settings)
    job_id = (await _only_job(factory)).id
    await _drain_until_dead(factory, job_id)

    for expected_index in (1, 2):
        revived = await _request(factory, fixture, settings=settings)
        assert revived.created is True, "a dead preview is retryable"
        assert (await _reservations(factory))[-1].revive_index == expected_index
        await _drain_until_dead(factory, job_id)

    with pytest.raises(FriendlyException) as refused:
        await _request(factory, fixture, settings=settings)

    assert refused.value.status_code == 429
    assert refused.value.data == {"reason": REASON_RETRY}
    # Three purchases (one original + two revives) and exactly that many times
    # MAX_ATTEMPTS provider calls. The loop has a ceiling, and it is arithmetic.
    assert len(await _reservations(factory)) == 3
    assert exploding.calls == 3 * MAX_ATTEMPTS
    # The refusal left the dead job dead rather than half-revived, so raising the
    # cap later still finds a terminal row to re-open.
    async with factory() as session:
        job = await session.get(AsyncJob, job_id)
    assert job is not None
    assert job.status == JOB_STATUS_DEAD


async def test_the_revive_cap_holds_with_the_cost_guard_off(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap is a safety bound, not a spending policy.

    `ENABLE_COST_GUARD` is off here (no `_use_quota`), so no daily budget is in
    force at all — and the loop is still bounded. A deployment that has not
    configured spending must not be the one where a failing provider can be
    re-bought forever.
    """
    _explodes(monkeypatch)
    settings = _revive_cap(1)
    fixture = await _seed_preview_ready_user(factory)
    await _request(factory, fixture, settings=settings)
    job_id = (await _only_job(factory)).id
    await _drain_until_dead(factory, job_id)

    await _request(factory, fixture, settings=settings)
    await _drain_until_dead(factory, job_id)
    with pytest.raises(FriendlyException) as refused:
        await _request(factory, fixture, settings=settings)

    assert refused.value.data == {"reason": REASON_RETRY}
    # Ledger rows are written even with no budget in force — they are what the
    # unconditional cap counts.
    assert len(await _reservations(factory)) == 2


async def test_a_revive_takes_its_own_reservation_and_can_be_refused_by_budget(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of acceptance 5: the press itself is metered.

    The revive is well within its cap, but the day's budget has been spent on a
    *different*, successful preview — so the retry is refused for the daily
    reason rather than the retry reason. Both are 429 and `data.reason` is what
    tells them apart.

    `test_v3_static_preview_jobs.py::test_reviving_a_dead_preview_goes_through_
    the_quota` owns the "a revive is metered at all" claim and was updated in
    place for the substrate move (its fixture seeded a succeeded `async_jobs` row;
    it now seeds a settled reservation, assertions unchanged). What is asserted
    HERE and only here is the pair: two 429s that a client must be able to tell
    apart, since one clears at midnight and the other never does.
    """
    _explodes(monkeypatch)
    fixture = await _seed_preview_ready_user(factory)
    await _request(factory, fixture)
    await _drain_until_dead(factory, (await _only_job(factory)).id)

    _use_quota(monkeypatch, limit=1)
    async with factory() as session:
        session.add(
            PaidGenerationReservation(
                user_id=fixture.user_id,
                job_type=JOB_TYPE,
                usage_day=datetime.now(UTC).date(),
                reservation_key=f"{JOB_TYPE}:unrelated:{uuid.uuid4()}",
                attempt_grant=MAX_ATTEMPTS,
                attempts_used=1,
                quality_regenerations=0,
                outcome=PaidGenerationOutcome.SUCCEEDED.value,
                settled_at=datetime.now(UTC),
            )
        )
        await session.commit()

    with pytest.raises(FriendlyException) as refused:
        await _request(factory, fixture)

    assert refused.value.status_code == 429
    assert refused.value.data == {"reason": REASON_DAILY}
    assert REASON_DAILY != REASON_RETRY


async def test_a_failed_generation_frees_its_unit(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V3-017 acceptance 3 still holds, and this is where it is visible.

    A dead render settles as `technical_failure`, which stops counting — so a
    provider outage does not lock the user out of a *different* preview. This is
    also the residual `request_static_preview`'s docstring declares: closing it
    means deciding that failures DO burn budget, which reverses a passed
    criterion and belongs to the spending-budget human gate.
    """
    _use_quota(monkeypatch, limit=1)
    _explodes(monkeypatch)
    fixture = await _seed_preview_ready_user(factory)
    await _request(factory, fixture)
    await _drain_until_dead(factory, (await _only_job(factory)).id)
    other = await _another_decision_for(factory, fixture)

    admitted = await _request(factory, other)

    assert admitted.created is True
    assert await _counted(factory, fixture.user_id) == 1, "only the live one counts"


# --- reservations converge -------------------------------------------------


async def test_a_successful_render_settles_as_succeeded(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    renderer: FakeStaticTryOn,
) -> None:
    """Settlement rides the drain tick that produced the terminal state, so the
    accounting does not wait for the reconcile cron."""
    fixture = await _seed_preview_ready_user(factory)
    await _request(factory, fixture)
    await drain_static_tryon_jobs(ctx=None)

    reservation = (await _reservations(factory))[0]
    assert reservation.outcome == PaidGenerationOutcome.SUCCEEDED.value
    assert reservation.attempts_used == 1
    assert reservation.quality_regenerations == 0
    assert reservation.settled_at is not None
    assert reservation.job_id is not None
    assert reservation.target_id is not None


async def test_a_quality_rejection_is_distinguishable_from_a_technical_failure(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
) -> None:
    """Acceptance 6, and the reason settlement joins the asset.

    V3-018 makes a quality rejection a **succeeded** job on purpose, so
    `AsyncJob.status` alone reports it identically to a good render. Seeded at the
    terminal state rather than by driving the gate, because what is under test is
    the accounting, not V3-018's verdict logic (`test_v3_visual_quality_jobs.py`
    owns that).
    """
    fixture = await _seed_preview_ready_user(factory)
    await _request(factory, fixture)
    job = await _only_job(factory)
    async with factory() as session:
        await session.execute(
            sa.update(AsyncJob)
            .where(AsyncJob.id == job.id)
            .values(status=JOB_STATUS_SUCCEEDED, attempts=2)
        )
        await session.execute(
            sa.update(OutfitVisualAsset)
            .where(OutfitVisualAsset.id == job.target_id)
            .values(
                generation_status="rejected_by_quality_gate",
                quality_signals={"render_count": 2},
            )
        )
        await session.commit()

    assert await reconcile_static_tryon_reservations() == 1

    reservation = (await _reservations(factory))[0]
    assert reservation.outcome == PaidGenerationOutcome.QUALITY_REJECTED.value
    assert reservation.quality_regenerations == 1, "the regeneration is counted, not the verdict"
    assert reservation.attempts_used == 2
    # And it is CHARGED, unlike a technical failure: the provider rendered twice.
    assert await _counted(factory, fixture.user_id) == 1


async def test_reconciliation_recovers_a_crashed_worker_with_nothing_from_it(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
) -> None:
    """Acceptance 7. The sweep is the recovery path for a worker that DIED, so it
    must work with nothing that worker wrote, and running it twice must settle
    nothing twice.

    The crash is staged the way `job_runtime` actually recovers one: the job is
    left `running` with an expired lease and out of attempts, then lease recovery
    dead-letters it. No worker ever reached a finalizer.
    """
    fixture = await _seed_preview_ready_user(factory)
    await _request(factory, fixture)
    job = await _only_job(factory)
    async with factory() as session:
        await session.execute(
            sa.update(AsyncJob)
            .where(AsyncJob.id == job.id)
            .values(
                status=JOB_STATUS_RUNNING,
                attempts=MAX_ATTEMPTS,
                lease_expires_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
        await session.commit()
    async with factory() as session:
        assert await recover_expired_leases(session) == 1
        await session.commit()

    assert await reconcile_static_tryon_reservations() == 1
    assert await reconcile_static_tryon_reservations() == 0, "idempotent"

    reservation = (await _reservations(factory))[0]
    assert reservation.outcome == PaidGenerationOutcome.TECHNICAL_FAILURE.value
    assert reservation.settled_at is not None
    assert await _counted(factory, fixture.user_id) == 0, "no budget left stuck behind a crash"


async def test_a_revive_closes_the_books_on_the_attempt_set_it_replaces(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    renderer: FakeStaticTryOn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two claims, one mechanism, and both would have been wrong without it.

    The generation is dead-lettered by *lease recovery* rather than by a drain
    tick — a crashed worker, the one case the drain tail cannot settle — so its
    reservation is still held when the user presses retry.

    1. **Accuracy.** `_revive` puts the job back to `queued`, so a reservation
       still held at that moment would later be settled from the *new* attempt's
       status: a generation that failed would be recorded as `succeeded`, and
       acceptance 6's distinction would quietly report the wrong thing.
    2. **Admissibility.** With a budget of one, the dead generation's own unit is
       what the retry needs back. Without the refund the user would get a 429
       they cannot act on until a cron ran.
    """
    _use_quota(monkeypatch, limit=1)
    fixture = await _seed_preview_ready_user(factory)
    await _request(factory, fixture)
    job = await _only_job(factory)
    async with factory() as session:
        await session.execute(
            sa.update(AsyncJob)
            .where(AsyncJob.id == job.id)
            .values(
                status=JOB_STATUS_RUNNING,
                attempts=MAX_ATTEMPTS,
                lease_expires_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
        await session.commit()
    async with factory() as session:
        assert await recover_expired_leases(session) == 1
        await session.commit()
    assert await _outcomes(factory) == [None], "still held: no drain tick ever ran"

    revived = await _request(factory, fixture)
    assert revived.created is True, "the refund arrived in time for the retry"
    await drain_static_tryon_jobs(ctx=None)

    assert await _outcomes(factory) == [
        PaidGenerationOutcome.TECHNICAL_FAILURE.value,
        PaidGenerationOutcome.SUCCEEDED.value,
    ], "the failed attempt-set stayed failed; only the revive succeeded"
    assert await _counted(factory, fixture.user_id) == 1


async def test_a_held_reservation_for_running_work_is_left_alone(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
) -> None:
    """The sweep converges terminal work and only terminal work.

    Settling a `running` job's reservation would refund budget for a provider
    call happening right now — the over-admission this feature closed,
    reintroduced from the other end.
    """
    fixture = await _seed_preview_ready_user(factory)
    await _request(factory, fixture)
    async with factory() as session:
        await session.execute(
            sa.update(AsyncJob).values(status=JOB_STATUS_RUNNING, attempts=1)
        )
        await session.commit()

    assert await reconcile_static_tryon_reservations() == 0
    assert await _outcomes(factory) == [None]
    assert await _counted(factory, fixture.user_id) == 1


async def test_two_identical_concurrent_requests_hold_one_unit_between_them(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One render, one unit — under a real race, asserted as an invariant.

    Both requests compose the same cache key, so they also compose the same
    `reservation_key`; whichever reaches the bucket lock second finds the
    reservation already there, replays it (`created=False`) and pays nothing.
    Serialised by the lock, so the outcome is the same under either order.
    """
    _use_quota(monkeypatch, limit=5)
    fixture = await _seed_preview_ready_user(factory)

    async def _attempt() -> uuid.UUID:
        return (await _request(factory, fixture)).asset_id

    first, second = await asyncio.gather(_attempt(), _attempt())

    assert first == second, "both requests answer with the same asset"
    assert len(await _assets(factory)) == 1
    assert await _outcomes(factory) == [None], "exactly one held unit behind one render"


async def test_a_lost_revive_race_refunds_the_unit_it_reserved(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INSTRUMENT (see the module docstring): `_revive` is patched to lose.

    Reachable for real — two retries of the same dead preview, where the second
    one's conditional UPDATE finds the job already `queued` — but which of the
    two loses depends on lock arrival order, so the branch is forced rather than
    raced. What it pins is the refund: the loser reserved a unit for work it did
    not start, and holding it would leak budget on every lost retry.
    """
    _explodes(monkeypatch)
    fixture = await _seed_preview_ready_user(factory)
    await _request(factory, fixture)
    await _drain_until_dead(factory, (await _only_job(factory)).id)

    async def _lost(self: RequestStaticPreview, job: AsyncJob) -> bool:
        return False

    monkeypatch.setattr(RequestStaticPreview, "_revive", _lost)
    result = await _request(factory, fixture)

    assert result.created is False, "the honest answer: this request started nothing"
    assert await _outcomes(factory) == [
        PaidGenerationOutcome.TECHNICAL_FAILURE.value,
        PaidGenerationOutcome.ABANDONED.value,
    ]
    assert await _counted(factory, fixture.user_id) == 0


async def test_a_lost_insert_race_refunds_the_unit_it_reserved(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INSTRUMENT (see the module docstring): the cache read is patched to miss.

    This branch is the reason a held reservation can never exist without a job
    behind it. It needs an asset whose `cache_key` is taken while THIS request's
    reservation key is free — which after V3-026 means an asset written by a
    non-reserving writer, i.e. a row that predates this feature. Staged exactly
    that way: a real preview is created, its reservation is deleted to make the
    asset look pre-V3-026, and the cache read is then forced to miss once.

    Without the release, that unit would be held forever: settlement joins
    through `job_id`, and this reservation never gets one.
    """
    _use_quota(monkeypatch, limit=5)
    fixture = await _seed_preview_ready_user(factory)
    await _request(factory, fixture)
    async with factory() as session:
        await session.execute(sa.delete(PaidGenerationReservation))
        await session.commit()
    assert await _reservations(factory) == []

    original = RequestStaticPreview._cached_asset
    calls = {"n": 0}

    async def _miss_once(self: RequestStaticPreview, key: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await original(self, key)  # type: ignore[arg-type]

    monkeypatch.setattr(RequestStaticPreview, "_cached_asset", _miss_once)
    result = await _request(factory, fixture)

    assert len(await _assets(factory)) == 1, "no second asset was created"
    assert result.asset_id == (await _assets(factory))[0].id
    assert await _outcomes(factory) == [PaidGenerationOutcome.ABANDONED.value]
    assert await _counted(factory, fixture.user_id) == 0, "the unit went back"


async def test_no_held_reservation_is_ever_left_without_its_job(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    renderer: FakeStaticTryOn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invariant the two refund paths exist to keep total.

    Settlement finds work through `job_id`, so a held reservation with none would
    be budget nothing can ever converge. Walked over a mixed day: a succeeded
    render (counts 1), a dead one (refunded, counts 0), a queued one (counts 1)
    and then a request refused because those two live units fill a budget of two.
    """
    _use_quota(monkeypatch, limit=2)
    first = await _seed_preview_ready_user(factory)
    await _request(factory, first)
    await drain_static_tryon_jobs(ctx=None)

    second = await _another_decision_for(factory, first)
    _explodes(monkeypatch)
    await _request(factory, second)
    await _drain_until_dead(
        factory,
        next(job.id for job in await _jobs(factory) if job.status != JOB_STATUS_SUCCEEDED),
    )

    third = await _another_decision_for(factory, first)
    await _request(factory, third)
    fourth = await _another_decision_for(factory, first)
    with pytest.raises(FriendlyException):
        await _request(factory, fourth)

    reservations = await _reservations(factory)
    assert len(reservations) == 3
    for reservation in reservations:
        assert reservation.job_id is not None, "a held unit always names its job"
        assert reservation.target_id is not None


# --- the flag off reserves nothing ----------------------------------------


async def test_the_flag_off_reserves_nothing(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 8. The refusal is first, before anything is read, so there is
    no asset, no job and no reservation — contract §2 rollback rule 1.

    The router turns this refusal into 503 (`test_v3_visual_preview_endpoint.py`
    owns the HTTP half); what is asserted here is that the ledger is untouched,
    which a 503 assertion cannot see.
    """
    _use_quota(monkeypatch, limit=5)
    fixture = await _seed_preview_ready_user(factory)
    off = get_settings().model_copy(
        update={"ENABLE_V3_STATIC_TRYON": False, "ENABLE_V3_AVATAR": True}
    )

    with pytest.raises(PreviewRefused) as refused:
        await _request(factory, fixture, settings=off)

    assert refused.value.reason is PreviewRefusal.FEATURE_DISABLED
    assert await _reservations(factory) == []
    assert await _jobs(factory) == []
    assert await _assets(factory) == []


async def test_a_refusal_before_the_cache_read_reserves_nothing(
    factory: async_sessionmaker[AsyncSession],
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same property for the other refusals: an avatar that is not ready.

    `_resolve_inputs` is ordered cheapest-refusal-first, all of it before the
    budget is touched, so a request that could never render never costs a unit.

    Both columns move together because the database says they must:
    `ck_avatar_profiles_active_version_matches_status` makes
    `status ∈ {ready, refresh_recommended}` equivalent to
    `active_version_id IS NOT NULL`, so a profile cannot be un-readied while
    still naming an active version. (Learned from CI rather than assumed — the
    first version of this fixture set the status alone and was rejected by that
    CHECK.)
    """
    _use_quota(monkeypatch, limit=5)
    fixture = await _seed_preview_ready_user(factory)
    async with factory() as session:
        await session.execute(
            sa.update(AvatarProfile)
            .where(AvatarProfile.id == fixture.profile_id)
            .values(status="pending_upload", active_version_id=None)
        )
        await session.commit()

    with pytest.raises(PreviewRefused) as refused:
        await _request(factory, fixture)

    assert refused.value.reason is PreviewRefusal.AVATAR_NOT_READY
    assert await _reservations(factory) == []


# --- the primitive itself -------------------------------------------------


async def test_reserve_is_idempotent_on_its_key(db_session: AsyncSession) -> None:
    """A replayed purchase returns the same reservation and consumes nothing.

    Which is also why `Admission.created` exists: releasing a reservation this
    call did not create would refund the request that won the race.
    """
    user_id = await _seed_user(db_session)
    key = f"{JOB_TYPE}:{uuid.uuid4().hex}"

    first = await reserve(
        db_session,
        user_id=user_id,
        job_type=JOB_TYPE,
        reservation_key=key,
        attempt_grant=MAX_ATTEMPTS,
        limit=1,
    )
    second = await reserve(
        db_session,
        user_id=user_id,
        job_type=JOB_TYPE,
        reservation_key=key,
        attempt_grant=MAX_ATTEMPTS,
        limit=1,
    )

    assert first.created is True
    assert second.created is False
    assert second.reservation_id == first.reservation_id
    assert await counted_reservations_today(db_session, user_id, JOB_TYPE) == 1


async def test_no_configured_limit_still_writes_the_ledger(db_session: AsyncSession) -> None:
    """`limit=None` (cost guard off) enforces nothing and records everything —
    the ledger is what the unconditional revive cap counts."""
    user_id = await _seed_user(db_session)
    for index in range(4):
        await reserve(
            db_session,
            user_id=user_id,
            job_type=JOB_TYPE,
            reservation_key=f"{JOB_TYPE}:{uuid.uuid4().hex}:{index}",
            attempt_grant=MAX_ATTEMPTS,
            limit=None,
        )

    assert await counted_reservations_today(db_session, user_id, JOB_TYPE) == 4


async def test_settling_with_no_terminal_work_is_a_no_op(db_session: AsyncSession) -> None:
    assert await settle_terminal_reservations(db_session, job_type=JOB_TYPE) == 0
