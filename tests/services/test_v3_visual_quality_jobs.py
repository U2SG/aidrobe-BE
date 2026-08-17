"""V3-018 — the quality gate's four acceptance criteria, executed end to end.

`test_v3_visual_quality_rules.py` proves the gate *decides* correctly, with no
database. This file proves the decision is *enforced* where it matters: that a
low-quality render is never uploaded and never signable through the API, that
the automatic-regeneration budget is exactly the configured cap, that a gate
outage is a technical failure and not a verdict, and that the persisted signals
survive re-validation against the acceptance-4 allowlist.

The three `evidence_required` items map onto sections below:

| evidence                    | section                                     |
|-----------------------------|---------------------------------------------|
| quality fixture matrix      | tests/unit/test_v3_visual_quality_rules.py  |
| hidden-low-quality API test | "a hidden render is unreachable"            |
| retry-cap test              | "the regeneration budget is the cap"        |

Harness is `test_v3_static_preview_jobs.py`'s, imported rather than copied so
the two files cannot drift: same session-factory patch, same storage double,
same seeded preview-ready user. The renderer doubles here are *quality*
failures — a `_BlankTryOn` whose output decodes perfectly and contains
nothing — where V3-017's doubles were *technical* failures that raise.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.avatar.candidate_rules import ForbiddenQualitySignalError
from app.application.ports.static_tryon import (
    PipelineIdentity,
    StaticTryOnRequest,
    StaticTryOnResult,
)
from app.application.ports.visual_quality_gate import RenderQualityObservation
from app.application.purchase.request_static_preview import JOB_TYPE as PREVIEW_JOB_TYPE
from app.application.purchase.visual_quality_rules import (
    DEFERRED_CHECKS,
    validate_render_signals,
)
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.session import get_db_session
from app.infrastructure.vision.fake_static_tryon import (
    MODEL_VERSION,
    PREVIEW_SIZE,
    PROMPT_VERSION,
    PROVIDER,
    FakeStaticTryOn,
)
from app.infrastructure.vision.fake_visual_quality_gate import FakeVisualQualityGate
from app.infrastructure.workers import static_tryon_worker
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_DEAD,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    claim_one,
    recover_expired_leases,
)
from app.infrastructure.workers.static_tryon_worker import (
    STATUS_FAILED,
    STATUS_QUALITY_CHECK,
    STATUS_READY,
    STATUS_REJECTED,
    drain_static_tryon_jobs,
    reconcile_dead_static_tryon_assets,
)
from app.presentation.api.v1.routers import outfit_visual as visual_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers
from tests.services.test_v3_static_preview_jobs import (
    _asset,
    _assets,
    _drain_until_dead,
    _FakeStorage,
    _Fixture,
    _jobs,
    _only_job,
    _request,
    _seed_preview_ready_user,
)
from tests.services.test_v3_static_preview_jobs import (
    factory as factory,  # noqa: PLC0414 — re-exported pytest fixtures
)
from tests.services.test_v3_static_preview_jobs import (
    renderer as renderer,
)
from tests.services.test_v3_static_preview_jobs import (
    storage as storage,
)

#: The frozen §6.2 GET shape. Asserted as an exact key set so `quality_signals`
#: (or anything else internal) growing into the response is a red test, not a
#: contract amendment nobody wrote. `created_at`/`expires_at` are V3-019's
#: ADDITIVE pair — §6.2 marks them additive, so admitting them here IS the
#: written amendment, not a weakening.
_FROZEN_RESPONSE_KEYS = {
    "asset_id",
    "generation_status",
    "image_url",
    "visual_type",
    "error_code",
    "error_message",
    "created_at",
    "expires_at",
}


# --- doubles -----------------------------------------------------------------


def _blank_png() -> bytes:
    """Full-size, valid, and empty: the render the gate exists to catch.

    It decodes, it is exactly the canonical canvas, and it has zero tonal
    variation — so of the three required checks only `render_content` fails,
    which is what "technically successful provider response, unusable picture"
    looks like in practice.
    """
    buffer = io.BytesIO()
    Image.new("RGB", PREVIEW_SIZE, (128, 128, 128)).save(buffer, format="PNG")
    return buffer.getvalue()


_FAKE_IDENTITY = PipelineIdentity(
    provider=PROVIDER, model_version=MODEL_VERSION, prompt_version=PROMPT_VERSION
)


class _BlankTryOn:
    """A provider that succeeds technically and fails visually, every time.

    Carries the fake pipeline's identity so `_assert_identity_matches` passes —
    these tests are about the *quality* path, and an identity mismatch would
    divert into the technical-failure path and prove nothing about the gate.
    """

    def __init__(self) -> None:
        self.calls = 0

    @property
    def pipeline_identity(self) -> PipelineIdentity:
        return _FAKE_IDENTITY

    async def render(self, request: StaticTryOnRequest) -> StaticTryOnResult:
        self.calls += 1
        return StaticTryOnResult(image=_blank_png(), identity=_FAKE_IDENTITY)


class _RecoveringTryOn:
    """Blank on the first render, real on the regeneration — the stochastic
    provider the regeneration loop exists for, made deterministic."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def pipeline_identity(self) -> PipelineIdentity:
        return _FAKE_IDENTITY

    async def render(self, request: StaticTryOnRequest) -> StaticTryOnResult:
        self.calls += 1
        if self.calls == 1:
            return StaticTryOnResult(image=_blank_png(), identity=_FAKE_IDENTITY)
        return await FakeStaticTryOn().render(request)


class _RaisingGate:
    """A gate that is down. Its opinion is nothing; the render is unverified."""

    def __init__(self) -> None:
        self.calls = 0

    async def observe(
        self, rendered_image: bytes, request: StaticTryOnRequest
    ) -> RenderQualityObservation:
        self.calls += 1
        raise RuntimeError("quality gate is down")


def _use_renderer(monkeypatch: pytest.MonkeyPatch, port: Any) -> Any:
    monkeypatch.setattr(static_tryon_worker, "static_tryon", lambda: port)
    return port


def _use_regen_budget(monkeypatch: pytest.MonkeyPatch, budget: int) -> None:
    """Patched on the worker module, the consuming module — `get_settings` is
    `lru_cache`d and imported by name, so patching anywhere else would leave
    the worker reading the real default (the repo CLAUDE.md rule)."""
    patched = get_settings().model_copy(
        update={"TRYON_QUALITY_MAX_AUTO_REGENERATIONS": budget}
    )
    monkeypatch.setattr(static_tryon_worker, "get_settings", lambda: patched)


# --- the API surface, asserted through the real router -----------------------


def _build_visual_app(session: AsyncSession) -> FastAPI:
    """The real M10 GET over this session — acceptance 2 says "not exposed
    through image_url", and that endpoint is the only thing that signs one, so
    the claim is asserted where a client would actually read it."""
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db() -> AsyncSession:
        return session

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_settings] = lambda: Settings(APP_ENV="dev")
    app.include_router(visual_router.router)
    return app


async def _get_asset_over_http(
    maker: async_sessionmaker[AsyncSession], asset_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, Any]:
    async with maker() as session:
        app = _build_visual_app(session)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            resp = await client.get(
                f"/v1/outfit-visual-assets/{asset_id}", headers={"X-User-Id": str(user_id)}
            )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _rejected_fixture(
    maker: async_sessionmaker[AsyncSession],
) -> tuple[_Fixture, uuid.UUID]:
    """Request a preview whose every render is blank, and drain it to verdict."""
    fixture = await _seed_preview_ready_user(maker)
    result = await _request(maker, fixture)
    await drain_static_tryon_jobs(ctx=None)
    return fixture, result.asset_id


# --- a hidden render is unreachable (acceptance 1 + 2) ------------------------


async def test_a_low_quality_render_is_rejected_and_nothing_is_uploaded(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 2 is structural: the gate runs on the bytes in memory, before
    `put_object`, so a hidden render has no object, no key and nothing any
    read path could ever sign — not a filter over a stored image."""
    _use_renderer(monkeypatch, _BlankTryOn())
    _, asset_id = await _rejected_fixture(factory)

    row = await _asset(factory, asset_id)
    assert row.generation_status == STATUS_REJECTED
    assert row.object_key is None
    assert row.image_url is None
    # §6.2's frozen shape: "null unless failed". A rejection is not a failure,
    # and FE renders it from `generation_status` alone (the degradation rule).
    assert row.error_code is None
    assert row.error_message is None
    # Nothing reached storage — not "uploaded then removed".
    assert storage.objects == {}

    # A rejection is a *succeeded* job: the worker did its work, the outcome is
    # a verdict. Failing it would hand `MAX_ATTEMPTS` to the quality loop.
    job = await _only_job(factory)
    assert job.status == JOB_STATUS_SUCCEEDED


async def test_the_hidden_render_is_unreachable_through_the_api(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hidden-low-quality API test: what a polling client actually sees.

    `rejected_by_quality_gate` is terminal (polling stops), `image_url` is
    null, `error_code` is null, and the response carries exactly the frozen
    §6.2 keys — `quality_signals` is an internal column and stays one.
    """
    _use_renderer(monkeypatch, _BlankTryOn())
    fixture, asset_id = await _rejected_fixture(factory)

    body = await _get_asset_over_http(factory, asset_id, fixture.user_id)

    assert body["generation_status"] == STATUS_REJECTED
    assert body["image_url"] is None
    assert body["error_code"] is None
    assert set(body) == _FROZEN_RESPONSE_KEYS


async def test_the_api_reports_quality_check_as_pending_not_terminal(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    renderer: FakeStaticTryOn,
) -> None:
    """§6.2's intermediate state, read side: a live job mid-gate polls onward.

    The row is placed on `quality_check` by hand with its job still queued —
    the in-process transition is too fast to observe from outside — and the
    endpoint must return it unchanged rather than projecting a terminal state.
    """
    fixture = await _seed_preview_ready_user(factory)
    result = await _request(factory, fixture)
    async with factory() as session:
        await session.execute(
            sa.update(OutfitVisualAsset)
            .where(OutfitVisualAsset.id == result.asset_id)
            .values(generation_status=STATUS_QUALITY_CHECK)
        )
        await session.commit()

    body = await _get_asset_over_http(factory, result.asset_id, fixture.user_id)

    assert body["generation_status"] == STATUS_QUALITY_CHECK
    assert body["image_url"] is None


# --- the regeneration budget is the cap (acceptance 3) ------------------------


async def test_the_default_budget_buys_exactly_one_regeneration(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract §6.3 verbatim: at most one model retry after a quality-gate
    failure, then `rejected_by_quality_gate`. Two renders, not three, not one."""
    blank = _use_renderer(monkeypatch, _BlankTryOn())
    _, asset_id = await _rejected_fixture(factory)

    assert blank.calls == 2
    row = await _asset(factory, asset_id)
    assert row.generation_status == STATUS_REJECTED
    assert row.quality_signals is not None
    assert row.quality_signals["render_count"] == 2
    assert row.quality_signals["regen_budget"] == 1


@pytest.mark.parametrize("budget", [0, 2])
async def test_the_regeneration_cap_is_the_explicit_configuration(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    budget: int,
) -> None:
    """Acceptance 3's escape clause, both directions: 0 disables regeneration
    entirely, and raising the budget is an operator decision that buys exactly
    `1 + budget` renders — not one more."""
    _use_regen_budget(monkeypatch, budget)
    blank = _use_renderer(monkeypatch, _BlankTryOn())
    _, asset_id = await _rejected_fixture(factory)

    assert blank.calls == budget + 1
    row = await _asset(factory, asset_id)
    assert row.generation_status == STATUS_REJECTED
    assert row.quality_signals is not None
    assert row.quality_signals["render_count"] == budget + 1
    assert row.quality_signals["regen_budget"] == budget


async def test_a_render_that_recovers_on_regeneration_reaches_ready(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget is a second chance, not a formality: a provider whose first
    frame is blank and whose second is real ends `ready`, with the accounting
    saying what the recovery cost."""
    recovering = _use_renderer(monkeypatch, _RecoveringTryOn())
    fixture = await _seed_preview_ready_user(factory)
    result = await _request(factory, fixture)

    await drain_static_tryon_jobs(ctx=None)

    assert recovering.calls == 2
    row = await _asset(factory, result.asset_id)
    assert row.generation_status == STATUS_READY
    assert row.object_key is not None
    assert row.object_key in storage.objects
    assert row.quality_signals is not None
    assert row.quality_signals["verdict"] == "show"
    assert row.quality_signals["render_count"] == 2

    body = await _get_asset_over_http(factory, result.asset_id, fixture.user_id)
    assert body["generation_status"] == STATUS_READY
    assert body["image_url"]


async def test_a_rejected_preview_is_served_from_cache_not_rerendered(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§6.3's "identical inputs never regenerate", applied to a render we
    refused to show: the job succeeded, so `_serve_cached` never revives it —
    the same key serves the same rejection and buys nothing. A new prompt or
    model version composes a new key; that is the designed escape hatch."""
    blank = _use_renderer(monkeypatch, _BlankTryOn())
    fixture, asset_id = await _rejected_fixture(factory)
    assert blank.calls == 2

    again = await _request(factory, fixture)

    assert again.asset_id == asset_id
    assert again.created is False
    assert again.generation_status == STATUS_REJECTED
    assert blank.calls == 2
    assert len(await _assets(factory)) == 1
    assert len(await _jobs(factory)) == 1


# --- a gate outage is not a verdict (fail closed, technically) ----------------


async def test_a_gate_that_raises_is_a_technical_failure_never_a_verdict(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    renderer: FakeStaticTryOn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed, on the correct axis. A gate outage must not ship renders
    unchecked (nothing was uploaded) — and it must not write
    `rejected_by_quality_gate` either, because that state means "a gate looked
    at this and said no". It rides the retry path and dead-letters `failed`."""
    gate = _RaisingGate()
    monkeypatch.setattr(static_tryon_worker, "visual_quality_gate", lambda: gate)
    fixture = await _seed_preview_ready_user(factory)
    result = await _request(factory, fixture)

    job = await _drain_until_dead(factory, (await _only_job(factory)).id)

    assert job.status == JOB_STATUS_DEAD
    assert gate.calls > 1  # the retries really re-judged, not one crash
    row = await _asset(factory, result.asset_id)
    assert row.generation_status == STATUS_FAILED
    assert row.error_code == "generation_failed"
    assert row.quality_signals is None
    # The gate sits before the upload, so an unverified render never landed.
    assert storage.objects == {}


async def test_a_worker_dead_mid_gate_converges_the_row_to_failed(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    renderer: FakeStaticTryOn,
) -> None:
    """§6.2 says polling stops only on terminal states, so `quality_check` must
    be reconciled exactly as `processing` is: a crash-loop that dies inside the
    gate leaves the row there, and the cron writes `failed` onto it."""
    fixture = await _seed_preview_ready_user(factory)
    result = await _request(factory, fixture)
    job = await _only_job(factory)
    async with factory() as session:
        await session.execute(
            sa.update(OutfitVisualAsset)
            .where(OutfitVisualAsset.id == result.asset_id)
            .values(generation_status=STATUS_QUALITY_CHECK)
        )
        await session.execute(
            sa.update(AsyncJob).where(AsyncJob.id == job.id).values(status=JOB_STATUS_DEAD)
        )
        await session.commit()

    reconciled = await reconcile_dead_static_tryon_assets(ctx=None)

    assert reconciled == 1
    row = await _asset(factory, result.asset_id)
    assert row.generation_status == STATUS_FAILED
    assert row.error_code == "generation_failed"


async def test_a_disowned_attempt_never_consults_the_gate(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `quality_check` write is fenced on the attempt still owning the job.

    V3-017's `test_a_recovered_lease_stops_the_original_attempt_writing_ready`
    fences the *terminal* write; this pins the *intermediate* one, and pins it
    at the earliest observable point — the gate is never even consulted. An
    unfenced `quality_check` from a disowned attempt could land after the new
    attempt's terminal write and strand a `succeeded` job's row on a
    non-terminal state no reconciler converges (they only touch dead jobs).

    The staging is V3-017's, one clock threaded through both calls: the lease
    recovery and the rival reclaim happen *inside* `render()`, because the
    render is the long call and mid-render is where a lease actually expires.
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
            return _FAKE_IDENTITY

        async def render(self, request: StaticTryOnRequest) -> StaticTryOnResult:
            async with self.maker() as session:
                await recover_expired_leases(session, now=lease_expiry)
                await session.commit()
            async with self.maker() as session:
                reclaimed = await claim_one(
                    session, [PREVIEW_JOB_TYPE], now=lease_expiry + timedelta(seconds=1)
                )
            self.reclaimed_attempts = None if reclaimed is None else reclaimed.attempts
            return await FakeStaticTryOn().render(request)

    class _CountingGate:
        def __init__(self) -> None:
            self.calls = 0
            self._real = FakeVisualQualityGate()

        async def observe(
            self, rendered_image: bytes, request: StaticTryOnRequest
        ) -> RenderQualityObservation:
            self.calls += 1
            return await self._real.observe(rendered_image, request)

    slow = _SlowTryOn(factory)
    gate = _CountingGate()
    monkeypatch.setattr(static_tryon_worker, "static_tryon", lambda: slow)
    monkeypatch.setattr(static_tryon_worker, "visual_quality_gate", lambda: gate)

    await drain_static_tryon_jobs(ctx=None)

    assert slow.reclaimed_attempts == 2, (
        "staging failed: the sweep did not re-queue the job into a second claim, "
        "so the attempt below was never actually disowned"
    )
    assert gate.calls == 0, "a disowned attempt must stop before the gate, not after"
    row = await _asset(factory, requested.asset_id)
    assert row.generation_status == "processing", (
        "the fenced intermediate write must not move the row"
    )
    assert row.quality_signals is None
    assert storage.objects == {}
    job = await _only_job(factory)
    assert job.status == JOB_STATUS_RUNNING, "the second claim still owns the job"
    assert job.attempts == 2


# --- the persisted record (acceptance 4, on the actual rows) ------------------


async def test_a_shown_render_records_the_gate_verdict_for_the_review(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    renderer: FakeStaticTryOn,
) -> None:
    """The human gate ("final visual quality threshold requires benchmark
    review") reviews *rows*: a shown render carries its verdict, the three
    determined checks, and the four §14 dimensions still honestly open."""
    fixture = await _seed_preview_ready_user(factory)
    result = await _request(factory, fixture)

    await drain_static_tryon_jobs(ctx=None)

    row = await _asset(factory, result.asset_id)
    assert row.generation_status == STATUS_READY
    signals = row.quality_signals
    assert signals is not None
    assert signals["verdict"] == "show"
    assert signals["render_count"] == 1
    assert set(signals["undetermined_checks"]) == set(DEFERRED_CHECKS)
    assert signals["failed_checks"] == []


@pytest.mark.parametrize("outcome", ["shown", "rejected"])
async def test_the_persisted_signals_survive_the_acceptance_4_validator(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """Quality logs contain no sensitive image data — re-proven on the payload
    that actually reached Postgres, both verdicts. `validate_render_signals`
    raising here would mean a URL, a blob, a person-judgement term or an
    off-allowlist key slipped past the construction-time check; the worker's
    log lines quote only keys from this same validated payload."""
    if outcome == "shown":
        _use_renderer(monkeypatch, FakeStaticTryOn())
    else:
        _use_renderer(monkeypatch, _BlankTryOn())
    fixture = await _seed_preview_ready_user(factory)
    result = await _request(factory, fixture)

    await drain_static_tryon_jobs(ctx=None)

    row = await _asset(factory, result.asset_id)
    assert row.quality_signals is not None
    try:
        validate_render_signals(dict(row.quality_signals))
    except ForbiddenQualitySignalError as exc:  # pragma: no cover - the failure message
        pytest.fail(f"persisted quality_signals broke acceptance 4: {exc}")
