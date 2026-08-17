"""V3-017 — the four acceptance criteria, executed rather than argued.

`test_v3_static_preview_cache_key.py` proves the key *decides* correctly, with
no database. This file proves the decision is *enforced*: that a second request
for the same preview does not buy a second render, that retries are bounded and
a dead job becomes visible on the row the user is watching, that a failed render
does not eat the daily budget, and that the flag off writes nothing at all.

The four `evidence_required` items map onto sections below:

| evidence         | section                                        |
|------------------|------------------------------------------------|
| idempotency race | "no duplicate paid job" (acceptance 1)         |
| lease recovery   | "bounded retries and dead letters" (accept. 2) |
| quota            | "what burns a call and what does not" (acc. 3) |
| flag             | "the flag off writes nothing" (acceptance 4)   |

**Everything here is a real execution.** `FakeStaticTryOn` is local and
deterministic (contract §6.4), so `queued → processing → ready` runs end to end
with real PNG bytes through a storage double — no mock handshake, no network, no
spend. What is *not* proven here, and cannot be: that a production model renders
a believable try-on. That is the budgeted, human-gated provider evaluation, the
same honest limit V3-013 and V3-016 recorded for their fakes.

Harness follows `test_v3_avatar_preparation_worker.py`: a session factory bound
to the test's Postgres, patched over the worker module's `AsyncSessionLocal`,
with fixture rows committed through it. The worker runs on a separate engine, so
uncommitted data would be invisible to it and the FK to `users` would fail.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.avatar.delete_avatar import DeleteAvatarProfile
from app.application.ports.static_tryon import (
    PipelineIdentity,
    StaticTryOnRequest,
    StaticTryOnResult,
)
from app.application.purchase.request_static_preview import (
    JOB_TYPE,
    MAX_ATTEMPTS,
    PreviewRefusal,
    PreviewRefused,
    RequestStaticPreview,
)
from app.application.purchase.static_preview_cache_key import build_cache_key
from app.common.enums import (
    AvatarStatus,
    Category,
    DecisionUncertainty,
    DecisionVisualNeed,
    PurchaseCandidateSource,
    PurchaseDecision,
    WardrobeDecisionType,
)
from app.common.exceptions import FriendlyException
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.avatar import AvatarProfile, AvatarVersion
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.purchase_decision import (
    PurchaseCandidate,
    WardrobeDecision,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.repositories.avatar_repo import AvatarLifecycle
from app.infrastructure.storage.object_key import static_preview_object_key
from app.infrastructure.vision.fake_static_tryon import (
    MODEL_VERSION,
    PROMPT_VERSION,
    PROVIDER,
    FakeStaticTryOn,
)
from app.infrastructure.workers import static_tryon_worker
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_DEAD,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    claim_one,
    recover_expired_leases,
)
from app.infrastructure.workers.static_tryon_worker import (
    drain_static_tryon_jobs,
    reconcile_dead_static_tryon_assets,
)

STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"


# --- harness ---------------------------------------------------------------


def _settings() -> Settings:
    """Both flags on. `model_copy` rather than `Settings(...)` so the rest of
    the environment's configuration is inherited unchanged."""
    return get_settings().model_copy(
        update={"ENABLE_V3_STATIC_TRYON": True, "ENABLE_V3_AVATAR": True}
    )


def _png(size: tuple[int, int] = (600, 900)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (170, 150, 130)).save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeStorage:
    """Serves a real PNG for any key and records what was written.

    Real bytes, not a sentinel, because `FakeStaticTryOn` actually opens and
    composites its inputs — a `b"x"` double would make the render raise and the
    happy path would be testing the failure branch.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.read_keys: list[str] = []
        self.deleted_keys: list[str] = []

    async def get_object(self, key: str) -> bytes:
        self.read_keys.append(key)
        return self.objects.get(key, _png())

    async def put_object(self, key: str, data: bytes, content_type: str) -> str:
        self.objects[key] = data
        return f"https://storage.test/{key}"

    async def delete_objects(self, keys: list[str]) -> None:
        """Idempotent, like the real port: a missing key is a success."""
        self.deleted_keys.extend(keys)
        for key in keys:
            self.objects.pop(key, None)


class _ExplodingTryOn:
    """A provider that is down. Used for the bounded-retry evidence."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def pipeline_identity(self) -> PipelineIdentity:
        return PipelineIdentity(
            provider=PROVIDER, model_version=MODEL_VERSION, prompt_version=PROMPT_VERSION
        )

    async def render(self, request: StaticTryOnRequest) -> StaticTryOnResult:
        self.calls += 1
        raise RuntimeError("provider is down")


class _FlakyTryOn:
    """Down until `heal()`, then renders. One object, so a retry can succeed.

    The alternative — patch exploding, then `monkeypatch.undo()` — would also
    undo the storage double, and the retry would write to real storage.
    """

    def __init__(self) -> None:
        self.healthy = False
        self.calls = 0

    @property
    def pipeline_identity(self) -> PipelineIdentity:
        return PipelineIdentity(
            provider=PROVIDER, model_version=MODEL_VERSION, prompt_version=PROMPT_VERSION
        )

    def heal(self) -> None:
        self.healthy = True

    async def render(self, request: StaticTryOnRequest) -> StaticTryOnResult:
        self.calls += 1
        if not self.healthy:
            raise RuntimeError("provider is down")
        return await FakeStaticTryOn().render(request)


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


def _use_quota(monkeypatch: pytest.MonkeyPatch, *, limit: int) -> None:
    """Turn the cost guard on with a small `tryon` limit.

    Patched on `app.application.cost_guard`, the *consuming* module, because
    `get_settings` is `lru_cache`d and bypasses `app.dependency_overrides` — the
    rule the repo's CLAUDE.md records and `test_cost_guard.py` follows. Patching
    anywhere else here would leave the guard reading the real settings, where
    `ENABLE_COST_GUARD` is False, and every quota assertion below would pass
    vacuously against a guard that never ran.
    """

    def _guard_settings() -> Settings:
        return Settings(
            APP_ENV="dev",
            ENABLE_COST_GUARD=True,
            TRYON_DAILY_CALL_LIMIT=limit,
        )

    monkeypatch.setattr("app.application.cost_guard.get_settings", _guard_settings)


# --- seeding ---------------------------------------------------------------


class _Fixture:
    """The ids a test needs to talk about a seeded, preview-ready user."""

    def __init__(
        self,
        user_id: uuid.UUID,
        profile_id: uuid.UUID,
        avatar_version_id: uuid.UUID,
        decision_id: uuid.UUID,
        candidate_object_key: str,
        item_ids: tuple[uuid.UUID, ...],
    ) -> None:
        self.user_id = user_id
        self.profile_id = profile_id
        self.avatar_version_id = avatar_version_id
        self.decision_id = decision_id
        self.candidate_object_key = candidate_object_key
        self.item_ids = item_ids


async def _seed_ready_avatar(
    session: AsyncSession, user_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """A `ready` profile pointing at a version with a canonical render.

    Built through `AvatarLifecycle.activate_version` rather than by inserting
    the end state: V3-011's `active_version_matches_status` CHECK refuses
    several hand-written combinations, and V3-012 lost a CI round to exactly
    that.
    """
    profile = AvatarProfile(user_id=user_id, status=AvatarStatus.NONE.value)
    session.add(profile)
    await session.flush()
    version = AvatarVersion(
        avatar_profile_id=profile.id,
        source_asset_ids=[],
        provider=PROVIDER,
        model_version=MODEL_VERSION,
        prompt_version=PROMPT_VERSION,
        canonical_front_object_key=f"users/{user_id}/avatar/canonical/front.png",
    )
    session.add(version)
    await session.flush()
    await AvatarLifecycle(session).activate_version(profile, version)
    return profile.id, version.id


async def _seed_garment(
    session: AsyncSession, user_id: uuid.UUID, *, category: str
) -> uuid.UUID:
    item = WardrobeItem(
        user_id=user_id,
        title=f"a {category}",
        category=category,
        color_main="navy",
    )
    session.add(item)
    await session.flush()
    session.add(
        WardrobeItemImage(
            item_id=item.id,
            image_url=f"https://storage.test/users/{user_id}/items/{item.id}.png",
            object_key=f"users/{user_id}/items/{item.id}.png",
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return item.id


async def _seed_preview_ready_user(
    maker: async_sessionmaker[AsyncSession],
    *,
    candidate_category: str = Category.OUTERWEAR.value,
) -> _Fixture:
    """A user who can legitimately ask for a preview, and a decision to ask about.

    The candidate is `outerwear` (slot `outer`) while the supporting garments
    fill `torso` and `legs`, so the rendered strip has no slot collision — the
    shape V3-006 actually composes.
    """
    user_id = uuid.uuid4()
    async with maker() as session:
        session.add(User(id=user_id))
        await session.flush()
        profile_id, version_id = await _seed_ready_avatar(session, user_id)

        top_id = await _seed_garment(session, user_id, category=Category.TOP.value)
        bottom_id = await _seed_garment(session, user_id, category=Category.BOTTOM.value)

        candidate_key = f"users/{user_id}/purchase/candidate-{uuid.uuid4()}.png"
        candidate = PurchaseCandidate(
            user_id=user_id,
            source_type=PurchaseCandidateSource.SCREENSHOT.value,
            source_object_key=candidate_key,
            parsed_item={"category": candidate_category, "colors": ["camel"]},
        )
        session.add(candidate)
        await session.flush()

        decision = WardrobeDecision(
            user_id=user_id,
            decision_type=WardrobeDecisionType.PURCHASE.value,
            candidate_id=candidate.id,
            context_snapshot={"concern": "volume"},
            evidence_snapshot={
                "outfit_evidence": {
                    "outfits": [
                        {
                            "slots": [
                                {"item_id": str(top_id), "slot": "torso"},
                                {"item_id": str(bottom_id), "slot": "legs"},
                            ]
                        }
                    ]
                }
            },
            decision=PurchaseDecision.WORTH_ADDING.value,
            uncertainty=DecisionUncertainty.LOW.value,
            visual_need=DecisionVisualNeed.RECOMMENDED.value,
        )
        session.add(decision)
        await session.flush()
        decision_id = decision.id
        await session.commit()

    return _Fixture(
        user_id=user_id,
        profile_id=profile_id,
        avatar_version_id=version_id,
        decision_id=decision_id,
        candidate_object_key=candidate_key,
        item_ids=(top_id, bottom_id),
    )


# --- helpers ---------------------------------------------------------------


async def _request(
    maker: async_sessionmaker[AsyncSession],
    fixture: _Fixture,
    *,
    settings: Settings | None = None,
    port: object | None = None,
):  # type: ignore[no-untyped-def]
    async with maker() as session:
        use_case = RequestStaticPreview(
            session,
            settings=settings or _settings(),
            static_tryon=port or FakeStaticTryOn(),  # type: ignore[arg-type]
        )
        result = await use_case.execute(
            user_id=fixture.user_id, decision_id=fixture.decision_id
        )
        await session.commit()
    return result


async def _assets(maker: async_sessionmaker[AsyncSession]) -> list[OutfitVisualAsset]:
    async with maker() as session:
        return list((await session.scalars(sa.select(OutfitVisualAsset))).all())


async def _jobs(maker: async_sessionmaker[AsyncSession]) -> list[AsyncJob]:
    async with maker() as session:
        return list(
            (await session.scalars(sa.select(AsyncJob).where(AsyncJob.job_type == JOB_TYPE))).all()
        )


async def _asset(
    maker: async_sessionmaker[AsyncSession], asset_id: uuid.UUID
) -> OutfitVisualAsset:
    async with maker() as session:
        row = await session.get(OutfitVisualAsset, asset_id)
    assert row is not None
    return row


async def _job(maker: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> AsyncJob:
    async with maker() as session:
        row = await session.get(AsyncJob, job_id)
    assert row is not None
    return row


async def _only_job(maker: async_sessionmaker[AsyncSession]) -> AsyncJob:
    jobs = await _jobs(maker)
    assert len(jobs) == 1, f"expected exactly one tryon job, found {len(jobs)}"
    return jobs[0]


async def _drain_until_dead(maker: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> AsyncJob:
    """Drain until the job is out of attempts, fast-forwarding the backoff.

    `mark_failed` schedules the next attempt 60s out and doubles from there, so
    a test that only drained would see one failure and stop. Moving
    `next_run_at` into the past is the only thing simulated: the attempts, the
    retries and the dead-letter are all real.
    """
    for _ in range(MAX_ATTEMPTS + 1):
        await drain_static_tryon_jobs(ctx=None)
        job = await _job(maker, job_id)
        if job.status == JOB_STATUS_DEAD:
            return job
        assert job.status == JOB_STATUS_QUEUED
        async with maker() as session:
            await session.execute(
                sa.update(AsyncJob)
                .where(AsyncJob.id == job_id)
                .values(next_run_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
    raise AssertionError("the preview job never dead-lettered")


# --- the happy path, so every failure test below has a control --------------


async def test_a_requested_preview_renders_and_reaches_ready(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage, renderer: FakeStaticTryOn
) -> None:
    """`queued → processing → ready`, end to end, with real bytes.

    Also the control for everything after it: if this ever goes red, the
    negative assertions elsewhere stop meaning what they claim.
    """
    fixture = await _seed_preview_ready_user(factory)

    requested = await _request(factory, fixture)
    assert requested.created is True
    assert requested.generation_status == "queued"

    assert await drain_static_tryon_jobs(ctx=None) == 1

    asset = await _asset(factory, requested.asset_id)
    assert asset.generation_status == STATUS_READY
    assert asset.object_key is not None
    assert asset.object_key.startswith(f"users/{fixture.user_id}/")
    # The provenance the cache key was composed from, on the row (migration
    # 0048's CHECK requires a `static_tryon` row to name its pipeline).
    assert (asset.provider, asset.model_version, asset.prompt_version) == (
        PROVIDER,
        MODEL_VERSION,
        PROMPT_VERSION,
    )
    assert (await _only_job(factory)).status == JOB_STATUS_SUCCEEDED

    # The avatar, the candidate and both garments were all read by their stable
    # keys, and a real PNG came back out.
    assert fixture.candidate_object_key in storage.read_keys
    with Image.open(io.BytesIO(storage.objects[asset.object_key])) as image:
        assert image.size[0] > 0


async def test_no_signed_url_or_image_byte_reaches_the_persisted_rows(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage, renderer: FakeStaticTryOn
) -> None:
    """Contract §7, on the artifacts that outlive log retention.

    `async_jobs.payload` and `outfit_visual_assets.input_snapshot` both persist,
    so this is a stronger check than a log assertion: they must carry stable
    object keys and item ids only.
    """
    fixture = await _seed_preview_ready_user(factory)
    requested = await _request(factory, fixture)
    await drain_static_tryon_jobs(ctx=None)

    job = await _only_job(factory)
    asset = await _asset(factory, requested.asset_id)
    serialized = f"{job.payload}{job.result}{job.last_error}{asset.input_snapshot}"

    assert "http" not in serialized
    assert "Signature" not in serialized
    assert "base64" not in serialized
    assert fixture.candidate_object_key in serialized


# --- acceptance 1: no duplicate paid job ------------------------------------


async def test_asking_twice_for_the_same_preview_buys_one_render(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage, renderer: FakeStaticTryOn
) -> None:
    """The common case, which is not a race: the user taps twice.

    Both the asset and the job are asserted, because either alone would miss a
    real failure — a second asset with the same job would still be a second row
    the user could watch, and a second job with one asset would be the paid
    duplicate acceptance 1 names.
    """
    fixture = await _seed_preview_ready_user(factory)

    first = await _request(factory, fixture)
    second = await _request(factory, fixture)

    assert second.asset_id == first.asset_id
    assert second.created is False, "a cache hit must not report that it started work"
    assert len(await _assets(factory)) == 1
    assert len(await _jobs(factory)) == 1
    # And the render itself ran once: a second queued job would drain here.
    assert await drain_static_tryon_jobs(ctx=None) == 1
    assert await drain_static_tryon_jobs(ctx=None) == 0


async def test_a_lost_insert_race_serves_the_winner_instead_of_paying_again(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    renderer: FakeStaticTryOn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `23505` branch, forced to execute rather than hoped for.

    The test above passes with the unique index dropped — the cache read
    catches it. This one blinds *only the first* cache read, which is exactly
    the state two concurrent requests are in when both find nothing, and then
    asserts the recovery path ran: `_cached_asset` is called a second time only
    inside `except IntegrityError`, so the call count is what distinguishes
    "the index resolved the race" from "the read did".
    """
    fixture = await _seed_preview_ready_user(factory)
    winner = await _request(factory, fixture)

    original = RequestStaticPreview._cached_asset
    calls = {"n": 0}

    async def _blind_first_read(
        self: RequestStaticPreview, key: object
    ) -> OutfitVisualAsset | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await original(self, key)  # type: ignore[arg-type]

    monkeypatch.setattr(RequestStaticPreview, "_cached_asset", _blind_first_read)

    loser = await _request(factory, fixture)

    assert calls["n"] >= 2, "the IntegrityError branch never ran — the read caught it instead"
    assert loser.asset_id == winner.asset_id
    assert loser.created is False
    assert len(await _assets(factory)) == 1
    assert len(await _jobs(factory)) == 1


async def test_two_concurrent_requests_produce_one_asset_and_one_job(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage, renderer: FakeStaticTryOn
) -> None:
    """The same race with nothing patched, on two real sessions.

    Weaker than the test above by construction — the two may serialize, in
    which case the cache read resolves it and the index is never reached — so
    it is here as the unmocked companion rather than as the proof. What it does
    cover that the forced version cannot: that losing the race raises nothing
    at the caller.
    """
    fixture = await _seed_preview_ready_user(factory)

    first, second = await asyncio.gather(
        _request(factory, fixture), _request(factory, fixture)
    )

    assert first.asset_id == second.asset_id
    assert len(await _assets(factory)) == 1
    assert len(await _jobs(factory)) == 1
    assert [first.created, second.created].count(True) == 1, (
        "exactly one of the two requests started the work"
    )


async def test_a_new_avatar_version_gets_its_own_preview(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage, renderer: FakeStaticTryOn
) -> None:
    """The other half of acceptance 1: the key must not over-collapse.

    `test_v3_static_preview_cache_key.py` proves the *digest* changes with the
    avatar version. This proves the digest is what the row is actually keyed
    on — a preview of the previous likeness must not be served to a replaced
    avatar, which is the one cache hit that would show a user someone else's
    render.
    """
    fixture = await _seed_preview_ready_user(factory)
    first = await _request(factory, fixture)

    async with factory() as session:
        profile = await session.get(AvatarProfile, fixture.profile_id)
        assert profile is not None
        replacement = AvatarVersion(
            avatar_profile_id=profile.id,
            source_asset_ids=[],
            provider=PROVIDER,
            model_version=MODEL_VERSION,
            prompt_version=PROMPT_VERSION,
            canonical_front_object_key=f"users/{fixture.user_id}/avatar/canonical/front-2.png",
        )
        session.add(replacement)
        await session.flush()
        await AvatarLifecycle(session).activate_version(profile, replacement)
        await session.commit()

    second = await _request(factory, fixture)

    assert second.asset_id != first.asset_id
    assert second.created is True
    assert len(await _assets(factory)) == 2
    assert len({job.idempotency_key for job in await _jobs(factory)}) == 2


async def test_the_row_is_keyed_on_the_frozen_contract_digest(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage, renderer: FakeStaticTryOn
) -> None:
    """`cache_key` on the row and `static_tryon:{digest}` on the job, verbatim.

    Recomputed here from contract §6.3's components rather than read back from
    the code that wrote it, so the two would have to be wrong in the same way
    to pass.
    """
    fixture = await _seed_preview_ready_user(factory)
    requested = await _request(factory, fixture)

    expected = build_cache_key(
        avatar_version_id=fixture.avatar_version_id,
        source_object_key=fixture.candidate_object_key,
        supporting_item_ids=list(fixture.item_ids),
        view_type="static_tryon",
        provider=PROVIDER,
        model_version=MODEL_VERSION,
        prompt_version=PROMPT_VERSION,
    )

    asset = await _asset(factory, requested.asset_id)
    assert asset.cache_key == expected.digest()
    assert (await _only_job(factory)).idempotency_key == expected.idempotency_key()


async def test_a_succeeded_preview_is_never_revived(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage, renderer: FakeStaticTryOn
) -> None:
    """Acceptance 1, at the exact point the retry path could have broken it.

    `_serve_cached` is what makes a dead preview re-runnable. If it treated
    `succeeded` as terminal too, every repeated request after a successful
    render would buy another one — the bound is the guarantee, so it is
    asserted directly rather than inferred from the dead-job tests.
    """
    fixture = await _seed_preview_ready_user(factory)
    requested = await _request(factory, fixture)
    await drain_static_tryon_jobs(ctx=None)
    succeeded = await _only_job(factory)
    assert succeeded.status == JOB_STATUS_SUCCEEDED

    repeat = await _request(factory, fixture)

    assert repeat.created is False
    assert repeat.asset_id == requested.asset_id
    unchanged = await _job(factory, succeeded.id)
    assert unchanged.status == JOB_STATUS_SUCCEEDED
    assert unchanged.attempts == succeeded.attempts
    # Nothing re-entered the queue, so nothing can be claimed and charged.
    assert await drain_static_tryon_jobs(ctx=None) == 0


# --- acceptance 2: bounded retries and observable dead letters --------------


async def test_a_retryable_failure_leaves_the_asset_processing(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blip with attempts left is not a failure the user should be shown.

    `mark_failed` puts the job back on the queue, so work really is still
    pending; rendering `failed` here would be a lie in the discouraging
    direction, and it is also what lets the eventual retry flip the row back.
    """
    exploding = _ExplodingTryOn()
    monkeypatch.setattr(static_tryon_worker, "static_tryon", lambda: exploding)
    fixture = await _seed_preview_ready_user(factory)
    requested = await _request(factory, fixture)

    await drain_static_tryon_jobs(ctx=None)

    job = await _only_job(factory)
    assert job.status == JOB_STATUS_QUEUED
    assert job.attempts == 1
    assert job.attempts < job.max_attempts
    asset = await _asset(factory, requested.asset_id)
    assert asset.generation_status == STATUS_PROCESSING
    assert asset.error_code is None


async def test_retries_are_bounded_and_the_dead_letter_reaches_the_asset(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 2, both halves: the retries stop, and the stop is visible.

    `MAX_ATTEMPTS` renders and no more — an unbounded worker against a paid
    provider is the failure this criterion exists to prevent — and the row the
    client polls says `failed` rather than spinning on `processing` forever.
    """
    exploding = _ExplodingTryOn()
    monkeypatch.setattr(static_tryon_worker, "static_tryon", lambda: exploding)
    fixture = await _seed_preview_ready_user(factory)
    requested = await _request(factory, fixture)

    dead = await _drain_until_dead(factory, (await _only_job(factory)).id)

    assert dead.status == JOB_STATUS_DEAD
    assert dead.attempts == MAX_ATTEMPTS
    assert exploding.calls == MAX_ATTEMPTS, "the provider was called once per attempt, no more"
    # The exception type only — never its text, and never the object keys the
    # payload carries. `async_jobs.last_error` outlives log retention.
    assert dead.last_error == "RuntimeError"
    assert fixture.candidate_object_key not in (dead.last_error or "")

    asset = await _asset(factory, requested.asset_id)
    assert asset.generation_status == STATUS_FAILED
    assert asset.error_code == "generation_failed"

    # And it stays stopped: a dead job is not claimable.
    assert await drain_static_tryon_jobs(ctx=None) == 0


async def test_a_recovered_lease_dead_letters_and_the_reconciler_writes_failed(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage, renderer: FakeStaticTryOn
) -> None:
    """The crash-loop path, where `mark_failed` never runs at all.

    A hard-killed worker leaves the row `running` with an expired lease;
    `recover_expired_leases` dead-letters it directly, so none of the worker's
    own failure handling fires and the asset is left on `processing` — the
    exact hole `reconcile_dead_static_tryon_assets` exists to close. Asserted
    at the asset row, because the job row is pre-existing shared machinery and
    the user is watching the asset.
    """
    fixture = await _seed_preview_ready_user(factory)
    requested = await _request(factory, fixture)

    # Claim it and then vanish, the way a killed worker does.
    async with factory() as session:
        claimed = await claim_one(session, [JOB_TYPE])
    assert claimed is not None
    async with factory() as session:
        await session.execute(
            sa.update(AsyncJob).where(AsyncJob.id == claimed.id).values(attempts=MAX_ATTEMPTS)
        )
        await session.commit()

    async with factory() as session:
        recovered = await recover_expired_leases(
            session, now=datetime.now(UTC) + timedelta(hours=1)
        )
        await session.commit()
    assert recovered == 1

    job = await _only_job(factory)
    assert job.status == JOB_STATUS_DEAD, "an expired lease at max attempts dead-letters"
    # Nothing has told the asset yet — this is the gap being closed. It is still
    # on `queued` rather than `processing` because the killed worker never got
    # as far as flipping the read model, which is the worse of the two shapes:
    # the reconciler's window has to cover both.
    assert (await _asset(factory, requested.asset_id)).generation_status == "queued"

    assert await reconcile_dead_static_tryon_assets(ctx=None) == 1

    asset = await _asset(factory, requested.asset_id)
    assert asset.generation_status == STATUS_FAILED
    assert asset.error_code == "generation_failed"
    # Idempotent: the second sweep finds nothing, because the row it would fix
    # is no longer in a pending status.
    assert await reconcile_dead_static_tryon_assets(ctx=None) == 0


async def test_a_recovered_lease_stops_the_original_attempt_writing_ready(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fencing branch: a stale finalizer must not resurrect a reclaimed job.

    The render runs long, the lease expires, the sweep returns the job to the
    queue and another worker claims it — and only then does the first attempt
    come back with an image. `mark_succeeded` is fenced on the claimed attempt,
    so it returns False and the `ready` write is rolled back. Without that, two
    workers would both finalize and the second attempt's render would land on
    an asset already reported ready by the first.

    The lease recovery happens *inside* `render()`, which is the genuine
    ordering rather than a shortcut: the worker really is mid-provider-call.

    **One clock, threaded through both calls.** `recover_expired_leases` stamps
    `next_run_at` with whatever `now` it is handed, so a sweep run on a fake
    future clock schedules the job at that future time. Reclaiming with the
    real clock then finds nothing to claim — the job looks an hour out — and
    the test silently degrades into "the render raised", which exercises the
    ordinary failure path instead of the fencing branch. So both calls read
    `_LEASE_EXPIRY`, and the reclaim is asserted in the test body rather than
    inside `render`: an assertion that fires inside the provider call would be
    swallowed by the worker's `except Exception` and reported as a render
    failure, hiding the fact that the test never staged what it claims to.
    """
    fixture = await _seed_preview_ready_user(factory)
    requested = await _request(factory, fixture)
    lease_expiry = datetime.now(UTC) + timedelta(hours=1)

    class _SlowTryOn:
        """Renders, but the world moves on while it does."""

        def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
            self.maker = maker
            self.reclaimed_attempts: int | None = None

        @property
        def pipeline_identity(self) -> PipelineIdentity:
            return PipelineIdentity(
                provider=PROVIDER, model_version=MODEL_VERSION, prompt_version=PROMPT_VERSION
            )

        async def render(self, request: StaticTryOnRequest) -> StaticTryOnResult:
            async with self.maker() as session:
                await recover_expired_leases(session, now=lease_expiry)
                await session.commit()
            async with self.maker() as session:
                reclaimed = await claim_one(
                    session, [JOB_TYPE], now=lease_expiry + timedelta(seconds=1)
                )
            self.reclaimed_attempts = None if reclaimed is None else reclaimed.attempts
            return await FakeStaticTryOn().render(request)

    slow = _SlowTryOn(factory)
    monkeypatch.setattr(static_tryon_worker, "static_tryon", lambda: slow)

    await drain_static_tryon_jobs(ctx=None)

    assert slow.reclaimed_attempts == 2, (
        "staging failed: the sweep did not re-queue the job into a second claim, "
        "so the finalizer below was never actually stale"
    )
    asset = await _asset(factory, requested.asset_id)
    assert asset.generation_status != STATUS_READY, (
        "a finalizer that lost its lease must not report the preview ready"
    )
    assert asset.generation_status == STATUS_PROCESSING
    assert asset.object_key is None, "the rolled-back transaction wrote no object key"

    job = await _only_job(factory)
    assert job.status == JOB_STATUS_RUNNING, "the second claim still owns the job"
    assert job.attempts == 2


async def test_a_render_finishing_after_the_avatar_purge_leaves_no_image(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    renderer: FakeStaticTryOn,
) -> None:
    """Revocation must win the race against a render already in flight.

    Contract §4.2: `DELETE /v1/avatar-profile?purge_derived=true` deletes the
    generated try-on assets. A render that started before the delete and lands
    after it is the one case the request-side sweep cannot see, because at sweep
    time the object does not exist yet.

    **The window is narrow and it is staged exactly.** A row deleted before
    `_assert_identity_matches` is caught there and no object is ever written;
    the only way to write an orphan is for the delete to land *after* that check
    and before the finalizer. So the delete runs inside `put_object` — which is
    the real long-running network call in that window, not an artificial hook.

    The prospective key the purge job already carries covers the same window
    from the other side (see `test_a_render_still_in_flight_is_swept_by_its_
    prospective_key`). Both exist because a worker crash between `put_object`
    and the finalizer would defeat this check alone, and a bucket write after
    the purge job drained would defeat that one alone.
    """
    fixture = await _seed_preview_ready_user(factory)
    requested = await _request(factory, fixture)
    expected_key = static_preview_object_key(fixture.user_id, requested.asset_id)
    original_put = storage.put_object

    async def _purge_then_put(key: str, data: bytes, content_type: str) -> str:
        # The genuine user action, through the real use case — not a hand-rolled
        # DELETE — so this fails if the deletion path stops removing the row.
        async with factory() as session:
            await DeleteAvatarProfile(session).execute(
                user_id=fixture.user_id, purge_derived=True
            )
            await session.commit()
        return await original_put(key, data, content_type)

    storage.put_object = _purge_then_put  # type: ignore[method-assign]

    await drain_static_tryon_jobs(ctx=None)

    assert expected_key not in storage.objects, (
        "the render landed after the user revoked their avatar and was kept"
    )
    assert expected_key in storage.deleted_keys
    assert await _assets(factory) == []
    job = await _only_job(factory)
    assert job.status != JOB_STATUS_SUCCEEDED, (
        "a render whose asset was deleted did not succeed"
    )
    assert job.last_error == "asset_not_found"


# --- acceptance 3: what burns a call and what does not ----------------------


async def test_the_quota_refuses_a_new_preview_once_the_daily_limit_is_spent(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    renderer: FakeStaticTryOn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is wired, and refusing costs the user nothing.

    A limit of 1 with one succeeded render already on the books, then a second
    *distinct* preview — distinct because two requests for the same one collide
    on the cache key and would never reach the guard.
    """
    _use_quota(monkeypatch, limit=1)
    first = await _seed_preview_ready_user(factory)
    await _request(factory, first)
    await drain_static_tryon_jobs(ctx=None)

    # A second decision for the same user, so the key differs but the bucket
    # does not.
    second = await _seed_preview_ready_user(factory)
    async with factory() as session:
        await session.execute(
            sa.update(WardrobeDecision)
            .where(WardrobeDecision.id == second.decision_id)
            .values(user_id=first.user_id)
        )
        await session.commit()
    second.user_id = first.user_id

    with pytest.raises(FriendlyException) as refused:
        await _request(factory, second)
    assert refused.value.status_code == 429

    # Refusal is free: no asset row for the second preview and no second job.
    assert len(await _assets(factory)) == 1
    assert len(await _jobs(factory)) == 1


async def test_a_dead_render_does_not_burn_the_daily_quota(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 3, stated exactly: "failed calls do not burn call quota".

    A limit of 1 and a render that dead-letters after its bounded retries. The
    call-count quota counts `succeeded`/`running` only, so the user is not
    charged for work that produced nothing — and the retry they will obviously
    attempt is admitted rather than refused with a 429 they cannot act on.
    """
    _use_quota(monkeypatch, limit=1)
    exploding = _ExplodingTryOn()
    monkeypatch.setattr(static_tryon_worker, "static_tryon", lambda: exploding)
    fixture = await _seed_preview_ready_user(factory)
    await _request(factory, fixture)
    dead = await _drain_until_dead(factory, (await _only_job(factory)).id)
    assert dead.status == JOB_STATUS_DEAD
    assert dead.attempts == MAX_ATTEMPTS, "three attempts, and none of them counted"

    # The whole budget is still there: the same user asks for a *different*
    # preview and is admitted.
    other = await _seed_preview_ready_user(factory)
    async with factory() as session:
        await session.execute(
            sa.update(WardrobeDecision)
            .where(WardrobeDecision.id == other.decision_id)
            .values(user_id=fixture.user_id)
        )
        await session.commit()
    other.user_id = fixture.user_id

    admitted = await _request(factory, other)
    assert admitted.created is True


async def test_a_cache_hit_costs_nothing_even_with_the_budget_spent(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    renderer: FakeStaticTryOn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordering the module calls load-bearing, asserted.

    Quota is checked *after* the cache read. Checking first would refuse to
    show a user the preview they already paid for, once they hit the daily cap
    — which spends nothing and shows nothing. This is also the case the Redis
    reservation path would get wrong, which is why it is not used here.
    """
    _use_quota(monkeypatch, limit=1)
    fixture = await _seed_preview_ready_user(factory)
    first = await _request(factory, fixture)
    await drain_static_tryon_jobs(ctx=None)
    assert (await _only_job(factory)).status == JOB_STATUS_SUCCEEDED  # the budget is now spent

    again = await _request(factory, fixture)

    assert again.asset_id == first.asset_id
    assert again.created is False
    assert len(await _jobs(factory)) == 1


async def test_reviving_a_dead_preview_goes_through_the_quota(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the same ordering: a revive is new paid work.

    A cache hit is free because nothing runs; reviving re-opens a job that will
    call the provider again, so it must be admitted like any other render. If
    it were not, the revive path would be an unmetered way to spend the
    provider budget — and it is reachable by simply pressing retry.
    """
    exploding = _ExplodingTryOn()
    monkeypatch.setattr(static_tryon_worker, "static_tryon", lambda: exploding)
    fixture = await _seed_preview_ready_user(factory)
    await _request(factory, fixture)
    dead = await _drain_until_dead(factory, (await _only_job(factory)).id)

    # Now spend the whole budget on something else, so the revive is the
    # request that finds the cupboard bare.
    _use_quota(monkeypatch, limit=1)
    async with factory() as session:
        session.add(
            AsyncJob(
                job_type=JOB_TYPE,
                idempotency_key=f"{JOB_TYPE}:unrelated:{uuid.uuid4()}",
                user_id=fixture.user_id,
                status=JOB_STATUS_SUCCEEDED,
                payload={},
            )
        )
        await session.commit()

    with pytest.raises(FriendlyException) as refused:
        await _request(factory, fixture)
    assert refused.value.status_code == 429

    # And the refusal left the dead job dead rather than half-revived: the
    # conditional UPDATE never ran, so a later retry within budget still has a
    # terminal row to re-open.
    assert (await _job(factory, dead.id)).status == JOB_STATUS_DEAD
    assert (await _job(factory, dead.id)).attempts == MAX_ATTEMPTS


async def test_a_dead_preview_can_be_retried_and_reaches_ready(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead preview is not a dead end — and the retry actually lands.

    The cache key never expires, so without the revival a `dead` row would own
    that key forever and every later request would answer 200 with a preview
    that will never arrive. V3-013 shipped exactly that bug; this asserts the
    retry runs to `ready` rather than merely that it was accepted.
    """
    flaky = _FlakyTryOn()
    monkeypatch.setattr(static_tryon_worker, "static_tryon", lambda: flaky)
    fixture = await _seed_preview_ready_user(factory)
    requested = await _request(factory, fixture)
    dead = await _drain_until_dead(factory, (await _only_job(factory)).id)
    assert flaky.calls == MAX_ATTEMPTS

    retried = await _request(factory, fixture)

    assert retried.created is True, "a retry that started work must not report a cache hit"
    assert retried.asset_id == requested.asset_id, "the revival re-opens the row"
    assert retried.generation_status == "queued"
    revived = await _job(factory, dead.id)
    assert revived.status == JOB_STATUS_QUEUED
    assert revived.attempts == 0, "a user-initiated retry gets the full attempt budget back"
    assert revived.last_error == "RuntimeError", "why they are retrying stays on the row"
    assert len(await _jobs(factory)) == 1, "the revival re-opened a row, it did not mint one"
    # The previous failure's message is cleared from the row the client polls.
    asset = await _asset(factory, requested.asset_id)
    assert asset.error_code is None
    assert asset.error_message is None

    flaky.heal()
    assert await drain_static_tryon_jobs(ctx=None) == 1
    assert (await _asset(factory, requested.asset_id)).generation_status == STATUS_READY


# --- acceptance 4: the flag off writes nothing ------------------------------


async def test_the_flag_off_prevents_enqueue(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage, renderer: FakeStaticTryOn
) -> None:
    """Acceptance 4, asserted as absence rather than as an exception type.

    "Prevents enqueue" is a claim about what is in the database, so raising the
    refusal is only half of it: no asset row the user could poll, no job a
    worker could claim, and nothing for the quota to have counted.
    """
    fixture = await _seed_preview_ready_user(factory)
    off = get_settings().model_copy(
        update={"ENABLE_V3_STATIC_TRYON": False, "ENABLE_V3_AVATAR": True}
    )

    with pytest.raises(PreviewRefused) as refused:
        await _request(factory, fixture, settings=off)
    assert refused.value.reason is PreviewRefusal.FEATURE_DISABLED

    assert await _assets(factory) == []
    assert await _jobs(factory) == []
    assert await drain_static_tryon_jobs(ctx=None) == 0


async def test_turning_the_flag_off_does_not_kill_work_already_admitted(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage, renderer: FakeStaticTryOn
) -> None:
    """Rollback rule 4's deliberate asymmetry, so it is a decision not an oversight.

    The flag gates the *request*; a job already queued when the flag flips off
    still finishes. Killing admitted paid work would leave the user's asset
    stuck on `processing` forever with the money already spent, and the rows
    persist either way (rule 3), so flipping back on restores function with no
    migration.
    """
    fixture = await _seed_preview_ready_user(factory)
    requested = await _request(factory, fixture)

    # The flag flips off here, between enqueue and drain — proven by a request
    # made in the new state being refused before the worker runs.
    off = get_settings().model_copy(
        update={"ENABLE_V3_STATIC_TRYON": False, "ENABLE_V3_AVATAR": True}
    )
    with pytest.raises(PreviewRefused):
        await _request(factory, fixture, settings=off)
    assert len(await _jobs(factory)) == 1, "no new work was admitted while off"

    # The already-admitted job still finishes: the worker does not re-read the
    # flag, which is the asymmetry under test.
    assert await drain_static_tryon_jobs(ctx=None) == 1
    assert (await _asset(factory, requested.asset_id)).generation_status == STATUS_READY


# --- the refusals that keep a doomed preview from ever costing anything -----


async def test_a_link_only_candidate_is_refused_before_anything_is_written(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage, renderer: FakeStaticTryOn
) -> None:
    """No garment image exists, so the render could only ever fail.

    Refused before the quota check and before any row is written, so a preview
    that cannot succeed never costs a call and never leaves the user watching a
    spinner that resolves to an error.
    """
    fixture = await _seed_preview_ready_user(factory)
    async with factory() as session:
        candidate_id = await session.scalar(
            sa.select(WardrobeDecision.candidate_id).where(
                WardrobeDecision.id == fixture.decision_id
            )
        )
        await session.execute(
            sa.update(PurchaseCandidate)
            .where(PurchaseCandidate.id == candidate_id)
            .values(
                source_type=PurchaseCandidateSource.SHARE_LINK.value,
                source_object_key=None,
                source_url="https://shop.example/product/1",
            )
        )
        await session.commit()

    with pytest.raises(PreviewRefused) as refused:
        await _request(factory, fixture)
    assert refused.value.reason is PreviewRefusal.NO_RENDERABLE_CANDIDATE

    assert await _assets(factory) == []
    assert await _jobs(factory) == []


async def test_a_user_without_a_ready_avatar_is_refused(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage, renderer: FakeStaticTryOn
) -> None:
    """Contract §6.1: no likeness, no preview — and nothing enqueued.

    Asserted through `disable`, the tombstone a deletion leaves, because that
    is the state where the profile row still exists and a status check is the
    only thing standing between a revoked avatar and a fresh render of it.
    """
    fixture = await _seed_preview_ready_user(factory)
    async with factory() as session:
        profile = await session.get(AvatarProfile, fixture.profile_id)
        assert profile is not None
        await AvatarLifecycle(session).disable(profile)
        await session.commit()

    with pytest.raises(PreviewRefused) as refused:
        await _request(factory, fixture)
    assert refused.value.reason is PreviewRefusal.AVATAR_NOT_READY

    assert await _assets(factory) == []
    assert await _jobs(factory) == []


async def test_another_users_decision_is_not_found_rather_than_forbidden(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage, renderer: FakeStaticTryOn
) -> None:
    """Contract §3: a probe must not learn that a decision id exists."""
    fixture = await _seed_preview_ready_user(factory)
    stranger = await _seed_preview_ready_user(factory)

    async with factory() as session:
        use_case = RequestStaticPreview(
            session, settings=_settings(), static_tryon=FakeStaticTryOn()
        )
        with pytest.raises(PreviewRefused) as refused:
            await use_case.execute(
                user_id=stranger.user_id, decision_id=fixture.decision_id
            )
    assert refused.value.reason is PreviewRefusal.DECISION_NOT_FOUND
    assert await _assets(factory) == []


async def test_a_decision_with_no_grounded_outfit_is_refused(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage, renderer: FakeStaticTryOn
) -> None:
    """V3-007 already reports `visual_need = none` here, so this is the backstop.

    Nothing to put on the avatar beside the candidate means the render would be
    a garment floating alone — refused rather than generated, and refused
    before any spend.
    """
    fixture = await _seed_preview_ready_user(factory)
    async with factory() as session:
        await session.execute(
            sa.update(WardrobeDecision)
            .where(WardrobeDecision.id == fixture.decision_id)
            .values(evidence_snapshot={"outfit_evidence": {"outfits": []}})
        )
        await session.commit()

    with pytest.raises(PreviewRefused) as refused:
        await _request(factory, fixture)
    assert refused.value.reason is PreviewRefusal.NO_GROUNDED_OUTFIT

    assert await _assets(factory) == []
    assert await _jobs(factory) == []


# --- payload handling -------------------------------------------------------


async def test_an_invalid_payload_fails_without_leaking_its_value(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage, renderer: FakeStaticTryOn
) -> None:
    """A malformed payload is permanently bad, and the error says only that.

    `uuid.UUID("…")` quotes the offending value back in its message, and
    `async_jobs.last_error` is a persisted column that outlives log retention —
    for this job type the payload carries storage object keys.
    """
    leaking = "users/secret-user/purchase/candidate.png"
    async with factory() as session:
        session.add(
            AsyncJob(
                job_type=JOB_TYPE,
                idempotency_key=f"{JOB_TYPE}:broken:{uuid.uuid4()}",
                payload={"user_id": "not-a-uuid", "candidate_object_key": leaking},
                max_attempts=1,
            )
        )
        await session.commit()

    await drain_static_tryon_jobs(ctx=None)

    job = await _only_job(factory)
    assert job.last_error == "invalid_payload"
    assert "not-a-uuid" not in (job.last_error or "")
    assert leaking not in (job.last_error or "")
