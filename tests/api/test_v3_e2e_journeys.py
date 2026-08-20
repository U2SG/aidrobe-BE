"""V3-022 — the five V3 paths as one user walks them, end to end.

Every V3 surface already has a per-router API test. What none of them has is a
**journey**: one app with every V3 router mounted at once, one user, one
correlation id, and each step's input produced by the previous step's real
response rather than by a seeded row. That is the whole delta of this file, and
it is what the acceptance criteria are about — they are claims about what
survives a *sequence*, not about what one endpoint answers.

Concretely, what a journey shows that the per-router files cannot:

- `test_v3_purchase_candidates` / `test_v3_purchase_decision_endpoint` seed a
  `PurchaseCandidate` row directly (`_add_candidate`) and decide over it. Here
  the decision is taken over a candidate that came out of the real
  `POST /v1/purchase-candidates` parse, so "the no-avatar user completes a
  useful decision" is a statement about the pipeline, not about a fixture.
- `test_v3_visual_preview_endpoint` proves the create/poll wire shapes and
  mounts two routers. It never asks the question acceptance 2 asks: after a
  render dies, **is the decision still the same decision?** Nothing in the
  per-router suites re-reads §5.3 after a §6.2 failure, because no file has
  both surfaces and a decision worth re-reading.
- `test_v3_preview_feedback_endpoint` posts feedback to a seeded `static_tryon`
  asset. Here the asset id is the one the journey's own preview request
  produced, so the §6.1 → §6.2 → §6.1-feedback chain is joined by data flow.
- The flag tests each check their own surface. Acceptance 4 is about all of
  them at once *plus* what still works: this file flips the three flags off
  around live rows and asserts the V2 surfaces in the same breath (contract §2
  rollback rules 3 and 4).

What is real and what is substituted, stated plainly. REAL: the routers, the
ingest parse, the similarity/outfit/decision engines, the snapshot writes and
replays, the cache-key resolution, the job runtime, the worker drain, the
quality gate, the retry budget and the dead-letter path, Postgres. SUBSTITUTED:
the four ingest provider seams (`FakeVision` / `FakeOcr` / a stub link parser /
a stub object-key signer — `test_v3_purchase_candidates`' harness verbatim) and
object storage in the worker (`_FakeStorage`, the `test_v3_static_preview_jobs`
double). No provider is called and no real photo exists anywhere in this file.

Flag combinations are only ever all-on or all-off: `settings.py`'s
`model_validator` makes `ENABLE_V3_STATIC_TRYON=true` with
`ENABLE_V3_AVATAR=false` a startup failure by design (contract §2 rule 5), so a
"one flag off" matrix would be testing an unbootable deployment.

App-building convention, as everywhere else in this suite: the routers call
`get_settings()` directly and it is `lru_cache`d, so the flag has to be
monkeypatched on each *consuming* module, not just overridden as a dependency.
"""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.ports.static_tryon import (
    PipelineIdentity,
    StaticTryOnRequest,
    StaticTryOnResult,
)
from app.application.ports.visual_quality_gate import RenderQualityObservation
from app.application.purchase.request_static_preview import JOB_TYPE, MAX_ATTEMPTS
from app.common.enums import AvatarStatus, Category, PurchaseDecision
from app.config.settings import Settings, get_settings
from app.infrastructure.importers.link_parser import ProductPage
from app.infrastructure.ocr.fake_ocr import FakeOcr
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
from app.infrastructure.persistence.session import get_db_session
from app.infrastructure.vision.fake_static_tryon import (
    MODEL_VERSION,
    PROMPT_VERSION,
    PROVIDER,
)
from app.infrastructure.vision.fake_vision import FakeVision
from app.infrastructure.workers import static_tryon_worker
from app.infrastructure.workers.job_runtime import JOB_STATUS_DEAD, JOB_STATUS_QUEUED
from app.presentation.api.v1.routers import (
    avatar as avatar_router,
)
from app.presentation.api.v1.routers import (
    outfit_visual,
    purchase_candidates,
    visual_feedback,
    visual_preview,
    wardrobe,
)
from app.presentation.middlewares.correlation import CorrelationIdMiddleware
from app.presentation.middlewares.exception_handler import setup_exception_handlers

#: The frozen §6.1 create body.
PREVIEW_BODY: dict[str, Any] = {
    "view_type": "static_tryon",
    "concern": "overall",
    "consent": True,
}

#: The four decisions that are an *answer*. `insufficient_information` is the
#: honest non-answer, so acceptance 1's "useful decision" is exactly this set.
ANSWERED_DECISIONS = frozenset(
    {
        PurchaseDecision.WORTH_ADDING.value,
        PurchaseDecision.CONSIDER_ALTERNATE.value,
        PurchaseDecision.WAIT.value,
        PurchaseDecision.AVOID_DUPLICATE.value,
    }
)

#: Contract §6.2's two no-result terminals. FE renders them identically
#: (`visualPreviewMapping.classifyGenerationStatus`), which is why acceptance 2
#: names them together.
NO_RESULT_STATUSES = ("failed", "rejected_by_quality_gate")

PRODUCT_PAGE = ProductPage(
    image_url="https://shop.example.com/coat.jpg",
    text="Merino 羊毛 Coat — oversized, ideal for 通勤 — ¥1,299.00",
)


# --- harness ----------------------------------------------------------------


class _FlagState:
    """One mutable source for every module's `get_settings`.

    A journey flips the flags *mid-test* (acceptance 4), and the routers,
    `get_current_user` and the avatar router's dependency override all have to
    move together. Rebinding six monkeypatches per flip would be six chances to
    miss one and assert against a half-rolled-back deployment.
    """

    def __init__(self) -> None:
        self.enabled = True

    def settings(self) -> Settings:
        return Settings(
            APP_ENV="dev",
            ENABLE_V3_PURCHASE_DECISION=self.enabled,
            ENABLE_V3_AVATAR=self.enabled,
            ENABLE_V3_STATIC_TRYON=self.enabled,
        )


class _StubLinkParser:
    """`test_v3_purchase_candidates`' stub. Present so the ingest router can be
    built; the journeys use the screenshot source (see `_ingest_candidate`)."""

    async def extract_product_page(self, source_url: str) -> ProductPage:
        return PRODUCT_PAGE


def _signer(object_key: str) -> str | None:
    """A fake short-lived URL, the way the real signer mints one.

    Load-bearing for the parse: `FakeVision` keys off the URL, so a key with
    "pants" in it yields a black bottom and anything else a white top. The
    journeys therefore get a deterministic `parsed_item` without a provider.
    """
    return f"https://signed.example.com/{object_key}?token=stub"


def _png(size: tuple[int, int] = (600, 900)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (170, 150, 130)).save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeStorage:
    """Serves real PNG bytes for any key — `FakeStaticTryOn` opens and
    composites its inputs, so a sentinel would make the happy path raise and
    quietly become the failure path."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def get_object(self, key: str) -> bytes:
        return self.objects.get(key, _png())

    async def put_object(self, key: str, data: bytes, content_type: str) -> str:
        self.objects[key] = data
        return f"https://storage.test/{key}"

    async def delete_objects(self, keys: list[str]) -> None:
        for key in keys:
            self.objects.pop(key, None)


class _ExplodingTryOn:
    """A provider that is down, for the acceptance-2 failure journey."""

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


class _RejectingGate:
    """A deterministic HIDE verdict (`test_v3_visual_preview_endpoint`'s)."""

    async def observe(
        self, rendered_image: bytes, request: StaticTryOnRequest
    ) -> RenderQualityObservation:
        return RenderQualityObservation(decode_ok=False, checker="rejecting-gate-e2e")


@pytest_asyncio.fixture
async def session(postgres_url: str) -> AsyncIterator[AsyncSession]:
    """A session whose `commit()` really commits.

    conftest's `db_session` holds an open outer transaction, so its `commit()`
    only releases a savepoint and the worker — draining on its own engine —
    finds nothing to claim. Rows committed here are swept by conftest's
    `_truncate_between_tests`, like every other separate-engine write in this
    suite.
    """
    engine = create_async_engine(postgres_url, future=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as opened:
        yield opened
    await engine.dispose()


@pytest_asyncio.fixture
async def worker_env(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_FakeStorage]:
    """The worker's own engine plus the storage double."""
    engine = create_async_engine(postgres_url, future=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(static_tryon_worker, "AsyncSessionLocal", maker)
    storage = _FakeStorage()
    monkeypatch.setattr(static_tryon_worker, "object_storage", lambda: storage)
    try:
        yield storage
    finally:
        await engine.dispose()


@pytest.fixture
def flags(monkeypatch: pytest.MonkeyPatch) -> _FlagState:
    state = _FlagState()
    for module in (purchase_candidates, visual_preview, visual_feedback, avatar_router):
        monkeypatch.setattr(module, "get_settings", state.settings)
    return state


def _build_app(db: AsyncSession, state: _FlagState) -> FastAPI:
    """Every V3 surface plus the V2 witnesses, in one app.

    `outfit_visual` (M10 poll) and `wardrobe` are the V2 half: they carry no V3
    gate and acceptance 4 is partly a claim about them.
    """
    app = FastAPI()
    setup_exception_handlers(app)
    # The cache-hit emitter reads the correlation contextvar this middleware
    # binds; without it a journey's flow id would be None.
    app.add_middleware(CorrelationIdMiddleware)

    async def _db() -> AsyncSession:
        return db

    app.dependency_overrides[get_db_session] = _db
    app.dependency_overrides[get_settings] = state.settings
    app.dependency_overrides[purchase_candidates.get_vision_service] = lambda: FakeVision()
    app.dependency_overrides[purchase_candidates.get_ocr_service] = lambda: FakeOcr()
    app.dependency_overrides[purchase_candidates.get_link_parser] = lambda: _StubLinkParser()
    app.dependency_overrides[purchase_candidates.get_object_key_signer] = lambda: _signer

    app.include_router(purchase_candidates.router)
    app.include_router(purchase_candidates.wardrobe_decisions_router)
    app.include_router(visual_preview.router)
    app.include_router(visual_feedback.router)
    app.include_router(avatar_router.router)
    app.include_router(outfit_visual.router)
    app.include_router(wardrobe.router)
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


# --- seeding: a wardrobe, and optionally a likeness -------------------------


async def _add_item(
    db: AsyncSession, user_id: uuid.UUID, *, category: str, color: str, title: str
) -> uuid.UUID:
    item = WardrobeItem(user_id=user_id, title=title, category=category, color_main=color)
    db.add(item)
    await db.flush()
    db.add(
        WardrobeItemImage(
            item_id=item.id,
            image_url=f"https://storage.test/users/{user_id}/items/{item.id}.png",
            object_key=f"users/{user_id}/items/{item.id}.png",
            created_at=datetime.now(UTC),
        )
    )
    await db.flush()
    return item.id


async def _seed_user(db: AsyncSession, *, with_avatar: bool) -> uuid.UUID:
    """A user who owns a wearable bottom and shoes, and nothing like the
    candidate.

    The candidate parses as a *top* (see `_signer`), so a bottom and shoes are
    exactly what the composer needs to build one complete look around it — that
    is what makes the decision groundable and the preview possible. Deliberately
    no top: a near-duplicate would push redundancy up, which is a different
    (also valid) journey and not the one acceptance 1 and 3 describe.
    """
    user_id = uuid.uuid4()
    db.add(User(id=user_id))
    await db.flush()
    await _add_item(db, user_id, category=Category.BOTTOM.value, color="navy", title="trousers")
    await _add_item(db, user_id, category=Category.SHOES.value, color="brown", title="loafers")
    if with_avatar:
        profile = AvatarProfile(user_id=user_id, status=AvatarStatus.NONE.value)
        db.add(profile)
        await db.flush()
        version = AvatarVersion(
            avatar_profile_id=profile.id,
            source_asset_ids=[],
            provider=PROVIDER,
            model_version=MODEL_VERSION,
            prompt_version=PROMPT_VERSION,
            canonical_front_object_key=f"users/{user_id}/avatar/canonical/front.png",
        )
        db.add(version)
        await db.flush()
        await AvatarLifecycle(db).activate_version(profile, version)
    await db.commit()
    return user_id


# --- journey steps ----------------------------------------------------------


def _headers(user_id: uuid.UUID, flow_id: str) -> dict[str, str]:
    return {"X-User-Id": str(user_id), "X-Correlation-Id": flow_id}


async def _ingest_candidate(
    client: AsyncClient, user_id: uuid.UUID, flow_id: str
) -> dict[str, Any]:
    """Step 1, through the real ingest endpoint.

    The screenshot source rather than a link: a screenshot's durable reference
    is an object key, and §6.1 refuses a link candidate outright ("no image to
    render"), so a link journey could never reach the preview steps at all.
    """
    resp = await client.post(
        "/v1/purchase-candidates",
        headers=_headers(user_id, flow_id),
        json={
            "source_type": "screenshot",
            "source_asset_id": f"users/{user_id}/purchase/candidate-jacket.png",
        },
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def _decide(
    client: AsyncClient, user_id: uuid.UUID, flow_id: str, candidate_id: str
) -> dict[str, Any]:
    resp = await client.post(
        f"/v1/purchase-candidates/{candidate_id}/decision",
        headers=_headers(user_id, flow_id),
        json={"concern": "none"},
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def _replay(
    client: AsyncClient, user_id: uuid.UUID, flow_id: str, decision_id: str
) -> dict[str, Any]:
    resp = await client.get(
        f"/v1/wardrobe-decisions/{decision_id}", headers=_headers(user_id, flow_id)
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _assert_useful(decision: dict[str, Any], candidate: dict[str, Any]) -> None:
    """Acceptance 1's "useful", spelled out as the §5.3 payload.

    Positive witnesses only: the parse produced a category (so the ingest really
    ran), the verdict is an answer rather than the honest non-answer, and every
    layer V3_SPEC §5.4 requires carries content.
    """
    assert candidate["status"] == "parsed", candidate
    assert candidate["parsed_item"]["category"], candidate
    assert decision["decision"] in ANSWERED_DECISIONS, decision["decision"]
    assert 2 <= len(decision["reasons"]) <= 3
    assert all(reason["text"].strip() for reason in decision["reasons"])
    assert decision["decision_text"].strip()
    assert decision["boundary_note"].strip()
    assert decision["next_actions"]
    assert decision["closet_comparison"]["summary_text"].strip()
    assert decision["uncertainty"] in {"low", "medium", "high"}


def _content(decision: dict[str, Any]) -> dict[str, Any]:
    """A decision payload minus the one field that is *supposed* to differ.

    Found by running this file: a whole-payload byte comparison of two reads of
    the same immutable snapshot fails, and it fails for the right reason.
    Contract §5.3 says `closet_comparison.similar_items[].image_url` is signed
    at read time and never persisted, so two reads legitimately carry different
    signatures — asserting byte equality across reads would be asserting the
    opposite of the contract (and would also make the test depend on which
    storage backend the environment configures: a presigned S3 URL carries a
    query, a local one does not).

    So the signature is dropped and *everything else* is compared: the verdict,
    the server-rendered copy, the reasons, the uncertainty, the visual need, the
    boundary note, the actions, and the object path of every compared thumbnail.
    A decision that changed, or a comparison that pointed at a different object,
    still fails here.
    """
    copied: dict[str, Any] = json.loads(json.dumps(decision))
    comparison = copied.get("closet_comparison") or {}
    for item in comparison.get("similar_items") or []:
        url = item.get("image_url")
        if url:
            item["image_url"] = str(url).split("?")[0]
    return copied


async def _count(db: AsyncSession, model: Any, **where: Any) -> int:
    clauses = [getattr(model, column) == value for column, value in where.items()]
    total = await db.scalar(select(func.count()).select_from(model).where(*clauses))
    return int(total or 0)


async def _request_preview(
    client: AsyncClient, user_id: uuid.UUID, flow_id: str, decision_id: str
) -> Any:
    return await client.post(
        f"/v1/wardrobe-decisions/{decision_id}/visual-preview",
        headers=_headers(user_id, flow_id),
        json=PREVIEW_BODY,
    )


async def _poll(client: AsyncClient, user_id: uuid.UUID, flow_id: str, asset_id: str) -> dict[str, Any]:
    resp = await client.get(
        f"/v1/outfit-visual-assets/{asset_id}", headers=_headers(user_id, flow_id)
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def _drain_until_dead(db: AsyncSession, job_id: uuid.UUID) -> dict[str, Any]:
    """Real attempts, real retries, real dead-letter — only the backoff clock
    is moved (`mark_failed` schedules the next attempt 60 s out, so a test that
    merely drained would see one failure and stop). The
    `test_v3_static_preview_jobs` helper, on this file's session.

    Returns plain values rather than the ORM row on purpose: a later
    `expire_all()` (this journey does one, to re-read the asset the worker wrote
    on its own engine) turns any attribute read on an escaped instance into a
    lazy refresh, and a lazy refresh outside the async context is
    `MissingGreenlet` — the trap BE CLAUDE.md records and V3-003 already hit
    once. Found here the same way: this assertion failed with exactly that.
    """
    for _ in range(MAX_ATTEMPTS + 1):
        await static_tryon_worker.drain_static_tryon_jobs()
        db.expire_all()
        job = await db.get(AsyncJob, job_id)
        assert job is not None
        snapshot = {"status": job.status, "attempts": job.attempts, "last_error": job.last_error}
        if snapshot["status"] == JOB_STATUS_DEAD:
            return snapshot
        assert snapshot["status"] == JOB_STATUS_QUEUED, snapshot["status"]
        await db.execute(
            sa.update(AsyncJob)
            .where(AsyncJob.id == job_id)
            .values(next_run_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()
    raise AssertionError("the preview job never dead-lettered")


# --- acceptance 1 -----------------------------------------------------------


async def test_a_user_with_no_avatar_completes_a_useful_decision(
    session: AsyncSession, flags: _FlagState
) -> None:
    """Acceptance 1, as a journey: ingest → decide → the answer is complete, and
    nothing on the way asked for a photo.

    The refusal at the end is the point. A user with no likeness who *does* tap
    the preview entry (a stale probe, a skewed build) must be told "not now" in
    a way that leaves the judgment they already have untouched — so the replay
    is compared byte-for-byte across the refusal.
    """
    user_id = await _seed_user(session, with_avatar=False)
    flow_id = str(uuid.uuid4())
    app = _build_app(session, flags)

    async with _client(app) as client:
        candidate = await _ingest_candidate(client, user_id, flow_id)
        decision = await _decide(client, user_id, flow_id, candidate["id"])
        _assert_useful(decision, candidate)

        profile = await client.get("/v1/avatar-profile", headers=_headers(user_id, flow_id))
        refused = await _request_preview(client, user_id, flow_id, decision["id"])
        await session.commit()
        replayed = await _replay(client, user_id, flow_id, decision["id"])

    # No visual setup happened anywhere in the journey: the profile is `none`
    # and there is no material awaiting a decision.
    assert profile.status_code == 200
    assert profile.json()["status"] == AvatarStatus.NONE.value
    assert profile.json()["active_version"] is None
    assert profile.json()["candidate"] is None

    # The preview is refused as a classified state, not an error (§6.1).
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == 4091
    assert refused.json()["data"] == {"reason": "avatar_not_ready"}

    # …and the decision is the same decision afterwards.
    assert _content(replayed) == _content(decision)

    # Nothing was generated and nothing was charged for: a decision costs no
    # render, which is V3_SPEC §3.1's "decide first, visualize second" as a
    # measurable fact rather than a design intention.
    assert await _count(session, OutfitVisualAsset, user_id=user_id) == 0
    assert await _count(session, AsyncJob, user_id=user_id, job_type=JOB_TYPE) == 0


# --- acceptance 2 -----------------------------------------------------------


async def test_a_provider_failure_never_erases_the_decision(
    session: AsyncSession, flags: _FlagState, worker_env: _FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 2, first half. The provider is down for every attempt, the
    retries run out and the row the client polls says `failed` — and the §5.3
    replay is byte-identical to the one taken before the render was ever asked
    for.

    Both halves are asserted, because either alone is vacuous: a surviving
    decision proves nothing if the failure never happened, and a dead job
    proves nothing about the decision.
    """
    exploding = _ExplodingTryOn()
    monkeypatch.setattr(static_tryon_worker, "static_tryon", lambda: exploding)
    user_id = await _seed_user(session, with_avatar=True)
    flow_id = str(uuid.uuid4())
    app = _build_app(session, flags)

    async with _client(app) as client:
        candidate = await _ingest_candidate(client, user_id, flow_id)
        decision = await _decide(client, user_id, flow_id, candidate["id"])
        _assert_useful(decision, candidate)
        before = await _replay(client, user_id, flow_id, decision["id"])

        created = await _request_preview(client, user_id, flow_id, decision["id"])
        assert created.status_code == 200, created.text
        asset_id = created.json()["asset_id"]
        await session.commit()

        job_id = await session.scalar(
            select(AsyncJob.id).where(AsyncJob.job_type == JOB_TYPE, AsyncJob.user_id == user_id)
        )
        assert job_id is not None
        dead = await _drain_until_dead(session, job_id)

        session.expire_all()
        polled = await _poll(client, user_id, flow_id, asset_id)
        after = await _replay(client, user_id, flow_id, decision["id"])

    # The failure is real, bounded and observable.
    assert exploding.calls == MAX_ATTEMPTS
    assert dead["status"] == JOB_STATUS_DEAD
    assert dead["attempts"] == MAX_ATTEMPTS
    # The dead-letter records the exception TYPE and never the payload — the
    # object keys a render carries must not outlive log retention in a job row.
    assert dead["last_error"] == "RuntimeError"
    assert polled["generation_status"] == "failed"
    assert polled["image_url"] is None

    # The decision is untouched — same payload, and still exactly one row.
    assert _content(after) == _content(before)
    assert after["decision"] in ANSWERED_DECISIONS
    assert await _count(session, WardrobeDecision, user_id=user_id) == 1


async def test_a_quality_rejection_never_erases_the_decision(
    session: AsyncSession, flags: _FlagState, worker_env: _FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 2, second half — and the case that is *not* a technical
    failure. The provider rendered fine; the gate said do not show it.

    §6.2 freezes the wire shape that makes FE's "same copy as failed" rule
    possible: `rejected_by_quality_gate` with `image_url` AND `error_code` both
    null, so the client cannot accidentally style a hidden render as an error.
    The decision underneath is asserted the same way as the failure journey.
    """
    monkeypatch.setattr(static_tryon_worker, "visual_quality_gate", lambda: _RejectingGate())
    user_id = await _seed_user(session, with_avatar=True)
    flow_id = str(uuid.uuid4())
    app = _build_app(session, flags)

    async with _client(app) as client:
        candidate = await _ingest_candidate(client, user_id, flow_id)
        decision = await _decide(client, user_id, flow_id, candidate["id"])
        before = await _replay(client, user_id, flow_id, decision["id"])

        created = await _request_preview(client, user_id, flow_id, decision["id"])
        asset_id = created.json()["asset_id"]
        await session.commit()

        assert await static_tryon_worker.drain_static_tryon_jobs() == 1
        session.expire_all()
        polled = await _poll(client, user_id, flow_id, asset_id)
        after = await _replay(client, user_id, flow_id, decision["id"])

    assert polled["generation_status"] == "rejected_by_quality_gate"
    assert polled["image_url"] is None
    assert polled["error_code"] is None
    assert polled["generation_status"] in NO_RESULT_STATUSES
    assert _content(after) == _content(before)
    assert after["decision"] in ANSWERED_DECISIONS


# --- acceptance 3 -----------------------------------------------------------


async def test_an_avatar_ready_user_reaches_a_cached_preview_with_one_action(
    session: AsyncSession, flags: _FlagState, worker_env: _FakeStorage
) -> None:
    """Acceptance 3, counted rather than described.

    "One explicit action" has three measurable parts, and all three are here:
    nothing generates before the tap (zero try-on jobs while the decision is
    read), ONE POST is what starts it, and coming back is free — the second POST
    returns the same asset and adds no job row, so the cache is what answers.

    The feedback POST at the end is not decoration: it closes the §6.1 → §6.2 →
    §6.1-feedback chain on an asset id this journey produced itself, which is
    the join no per-router file can make.
    """
    # No provider patch here on purpose: the container's own default IS the P0
    # fake adapter, so this journey runs the same renderer production would call
    # through, one adapter swap away from a real provider.
    user_id = await _seed_user(session, with_avatar=True)
    flow_id = str(uuid.uuid4())
    app = _build_app(session, flags)

    async with _client(app) as client:
        candidate = await _ingest_candidate(client, user_id, flow_id)
        decision = await _decide(client, user_id, flow_id, candidate["id"])
        _assert_useful(decision, candidate)
        await session.commit()

        # Nothing implicit: reading a decision has started no render.
        assert await _count(session, AsyncJob, user_id=user_id, job_type=JOB_TYPE) == 0
        assert await _count(session, OutfitVisualAsset, user_id=user_id) == 0

        first = await _request_preview(client, user_id, flow_id, decision["id"])
        assert first.status_code == 200, first.text
        asset_id = first.json()["asset_id"]
        assert first.json()["generation_status"] == "queued"
        await session.commit()
        jobs_after_first = await _count(session, AsyncJob, user_id=user_id, job_type=JOB_TYPE)

        assert await static_tryon_worker.drain_static_tryon_jobs() == 1
        session.expire_all()
        ready = await _poll(client, user_id, flow_id, asset_id)

        # The user leaves and comes back. Same asset, no new work.
        second = await _request_preview(client, user_id, flow_id, decision["id"])
        await session.commit()
        jobs_after_second = await _count(session, AsyncJob, user_id=user_id, job_type=JOB_TYPE)

        feedback = await client.post(
            f"/v1/outfit-visual-assets/{asset_id}/feedback",
            headers=_headers(user_id, flow_id),
            json={"verdict": "looks_like_me"},
        )
        await session.commit()
        replayed = await _replay(client, user_id, flow_id, decision["id"])

    assert jobs_after_first == 1, "one tap enqueued exactly one render"
    assert ready["generation_status"] == "ready"
    assert ready["image_url"], "a ready render has something to show"
    assert ready["visual_type"] == "static_tryon"
    assert ready["created_at"] is not None

    assert second.status_code == 200
    assert second.json()["asset_id"] == asset_id, "the cache answered, not a new render"
    assert jobs_after_second == 1, "coming back enqueued nothing"
    assert await _count(session, OutfitVisualAsset, user_id=user_id) == 1

    assert feedback.status_code == 200, feedback.text
    assert feedback.json()["verdict"] == "looks_like_me"
    assert feedback.json()["asset_id"] == asset_id
    # §6.4: the provenance the metrics join on stays server-side.
    assert not {"provider", "model_version", "prompt_version", "object_key"} & set(feedback.json())

    # The preview was an addition to the decision, never a replacement for it.
    assert replayed["decision"] in ANSWERED_DECISIONS
    assert replayed["reasons"]


# --- acceptance 4 -----------------------------------------------------------


async def test_flags_off_hide_every_v3_surface_while_v2_keeps_serving(
    session: AsyncSession, flags: _FlagState, worker_env: _FakeStorage
) -> None:
    """Acceptance 4, first half: the rollback, taken around live rows.

    A flag-off test on an empty database only shows that a gate raises. This one
    builds a real decision and a real ready render first, so it can show the
    three things a rollback actually has to do: hide every new surface (rule 1),
    keep serving the V2 ones (rule 4), and lose nothing (rule 3).
    """
    user_id = await _seed_user(session, with_avatar=True)
    flow_id = str(uuid.uuid4())
    app = _build_app(session, flags)

    async with _client(app) as client:
        candidate = await _ingest_candidate(client, user_id, flow_id)
        decision = await _decide(client, user_id, flow_id, candidate["id"])
        created = await _request_preview(client, user_id, flow_id, decision["id"])
        asset_id = created.json()["asset_id"]
        await session.commit()
        assert await static_tryon_worker.drain_static_tryon_jobs() == 1
        session.expire_all()
        assert (await _poll(client, user_id, flow_id, asset_id))["generation_status"] == "ready"

        # --- the rollback ---
        flags.enabled = False

        gated = {
            "POST /v1/purchase-candidates": await client.post(
                "/v1/purchase-candidates",
                headers=_headers(user_id, flow_id),
                json={"source_type": "screenshot", "source_asset_id": f"users/{user_id}/x.png"},
            ),
            "GET /v1/purchase-candidates/{id}": await client.get(
                f"/v1/purchase-candidates/{candidate['id']}", headers=_headers(user_id, flow_id)
            ),
            "POST /v1/purchase-candidates/{id}/decision": await client.post(
                f"/v1/purchase-candidates/{candidate['id']}/decision",
                headers=_headers(user_id, flow_id),
                json={"concern": "none"},
            ),
            "GET /v1/wardrobe-decisions/{id}": await client.get(
                f"/v1/wardrobe-decisions/{decision['id']}", headers=_headers(user_id, flow_id)
            ),
            "GET /v1/avatar-profile": await client.get(
                "/v1/avatar-profile", headers=_headers(user_id, flow_id)
            ),
            "POST …/visual-preview": await _request_preview(
                client, user_id, flow_id, decision["id"]
            ),
            "POST …/feedback": await client.post(
                f"/v1/outfit-visual-assets/{asset_id}/feedback",
                headers=_headers(user_id, flow_id),
                json={"verdict": "looks_like_me"},
            ),
        }

        # The V2 witnesses, in the same flag state — "no V3 surface answers" and
        # "the app is down" are otherwise the same observation.
        v2_asset = await client.get(
            f"/v1/outfit-visual-assets/{asset_id}", headers=_headers(user_id, flow_id)
        )
        v2_wardrobe = await client.get(
            "/v1/wardrobe-items", headers=_headers(user_id, flow_id)
        )

    for label, response in gated.items():
        assert response.status_code == 503, f"{label} answered {response.status_code}"
        assert response.json()["code"] == 4003, label

    # Rule 4: the M10 poll is untouched by the flag — V2 outfit-board polling
    # depends on it, and a cached render stays readable.
    assert v2_asset.status_code == 200
    assert v2_asset.json()["generation_status"] == "ready"
    assert v2_asset.json()["image_url"]
    # A second V2 surface, so the witness is not just the one endpoint that
    # happens to share a prefix with a V3 router.
    assert v2_wardrobe.status_code == 200
    assert v2_wardrobe.json()["data"]["total"] == 2

    # Rule 3: exposure was gated, persistence was not.
    assert await _count(session, PurchaseCandidate, user_id=user_id) == 1
    assert await _count(session, WardrobeDecision, user_id=user_id) == 1
    assert await _count(session, OutfitVisualAsset, user_id=user_id) == 1


async def test_flipping_the_flags_back_on_restores_the_same_rows(
    session: AsyncSession, flags: _FlagState
) -> None:
    """Acceptance 4, second half — the return leg, which is the half that makes
    "rollback" mean anything.

    Contract §2 rule 3 says turning a flag back on restores function with no
    migration. So: capture the replay, flip off, flip on, replay again, and
    require the same bytes and the same ids. No migration is run here, and that
    is the assertion — the same code path serves the same rows again.
    """
    user_id = await _seed_user(session, with_avatar=False)
    flow_id = str(uuid.uuid4())
    app = _build_app(session, flags)

    async with _client(app) as client:
        candidate = await _ingest_candidate(client, user_id, flow_id)
        decision = await _decide(client, user_id, flow_id, candidate["id"])
        before = await _replay(client, user_id, flow_id, decision["id"])
        await session.commit()

        flags.enabled = False
        hidden = await client.get(
            f"/v1/wardrobe-decisions/{decision['id']}", headers=_headers(user_id, flow_id)
        )

        flags.enabled = True
        after = await _replay(client, user_id, flow_id, decision["id"])
        candidate_again = await client.get(
            f"/v1/purchase-candidates/{candidate['id']}", headers=_headers(user_id, flow_id)
        )
        # The same candidate can still be asked again, and §5.3's idempotency
        # means it answers with the decision it already had rather than a new
        # one — a rollback cycle did not make the user's history re-decidable.
        redecided = await _decide(client, user_id, flow_id, candidate["id"])

    assert hidden.status_code == 503
    assert _content(after) == _content(before)
    assert candidate_again.status_code == 200
    assert candidate_again.json()["id"] == candidate["id"]
    assert redecided["id"] == decision["id"]
    assert await _count(session, WardrobeDecision, user_id=user_id) == 1
