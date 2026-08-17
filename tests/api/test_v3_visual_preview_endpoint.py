"""V3-019 — `POST /v1/wardrobe-decisions/{id}/visual-preview` (contract §6.1).

The wire shape, the refusal vocabulary and the polling lifecycle, at the HTTP
layer. The pipeline itself is V3-017's (`tests/services/test_v3_static_preview_
jobs.py`) and the quality gate V3-018's; this file asserts what only a request
can show, mapped onto the feature's acceptance criteria:

1. *Create endpoint chooses a grounded outfit from the decision snapshot* —
   the enqueued job's garments are exactly the snapshot's slots, and the test
   seeds a wardrobe that has since "moved on" (an extra item the snapshot never
   mentioned) to prove the composition is a replay, not a recomputation.
2. *FE cannot arbitrarily submit an infinite outfit editor state in P0* — the
   frozen three-field body rejects any extra field with 422
   (`extra="forbid"`), so outfit state cannot even be smuggled, let alone
   honored.
3. *Polling stops on ready, failed or rejected quality states* — the BE half:
   `GET /v1/outfit-visual-assets/{asset_id}` reaches all three terminal states
   for a V3 preview, `ready` with a signed URL and `rejected_by_quality_gate`
   with `image_url` null AND `error_code` null (the frozen §6.2 shape — FE
   renders the rejection from the status alone). The FE half (the loop that
   actually stops, including on an unknown status) is
   `aidrobe-FE/scripts/visualPreviewMapping.test.ts`.
4. *No-result state returns to decision evidence without a dead end* — the BE
   contribution is that a refusal is a classified state, not a 500: the two
   "no preview possible" reasons ride `data.reason` so FE can fall back to the
   decision card quietly instead of toasting an error.

App-building convention copied from tests/api/test_v3_purchase_decision_
endpoint.py: the router calls `get_settings()` directly (lru-cached, so it
bypasses `app.dependency_overrides`), hence the flag is monkeypatched on the
consuming module.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.ports.static_tryon import StaticTryOnRequest
from app.application.ports.visual_quality_gate import RenderQualityObservation
from app.common.enums import (
    AvatarStatus,
    Category,
    DecisionUncertainty,
    DecisionVisualNeed,
    PurchaseCandidateSource,
    PurchaseDecision,
    WardrobeDecisionType,
)
from app.config.settings import Settings
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
from app.infrastructure.workers import static_tryon_worker
from app.presentation.api.v1.routers import outfit_visual, visual_preview
from app.presentation.middlewares.correlation import CorrelationIdMiddleware
from app.presentation.middlewares.exception_handler import setup_exception_handlers

FROZEN_BODY: dict[str, Any] = {
    "view_type": "static_tryon",
    "concern": "overall",
    "consent": True,
}


def _build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)
    # The router's cache-hit emitter reads the correlation contextvar this
    # middleware binds; without it the flow_id would be None and the emission
    # test would assert against a path that cannot run.
    app.add_middleware(CorrelationIdMiddleware)

    async def _db() -> AsyncSession:
        return session

    app.dependency_overrides[get_db_session] = _db
    app.include_router(visual_preview.router)
    # The polling half of the lifecycle — deliberately in the same app, because
    # acceptance 3 is about the pair of them.
    app.include_router(outfit_visual.router)
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _set_flag(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    monkeypatch.setattr(
        visual_preview,
        "get_settings",
        lambda: Settings(
            APP_ENV="dev",
            ENABLE_V3_STATIC_TRYON=enabled,
            ENABLE_V3_AVATAR=enabled,
        ),
    )


def _png(size: tuple[int, int] = (600, 900)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (170, 150, 130)).save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeStorage:
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


class _RejectingGate:
    """Reports bytes that do not decode — a deterministic HIDE verdict."""

    async def observe(self, rendered_image: bytes, request: StaticTryOnRequest) -> RenderQualityObservation:
        return RenderQualityObservation(decode_ok=False, checker="rejecting-gate-test")


# --- seeding (the test_v3_static_preview_jobs shapes, through the router) ----


async def _seed_ready_avatar(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
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
    return version.id


async def _seed_garment(session: AsyncSession, user_id: uuid.UUID, *, category: str) -> uuid.UUID:
    item = WardrobeItem(user_id=user_id, title=f"a {category}", category=category, color_main="navy")
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


async def _seed_decision(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    with_avatar: bool = True,
    candidate_object_key: str | None = "auto",
    source_url: str | None = None,
    outfits: list[dict[str, Any]] | str = "auto",
) -> uuid.UUID:
    """A user + candidate + decision snapshot, in whatever broken shape a test needs."""
    session.add(User(id=user_id))
    await session.flush()
    if with_avatar:
        await _seed_ready_avatar(session, user_id)
    top_id = await _seed_garment(session, user_id, category=Category.TOP.value)
    bottom_id = await _seed_garment(session, user_id, category=Category.BOTTOM.value)

    if candidate_object_key == "auto":
        candidate_object_key = f"users/{user_id}/purchase/candidate-{uuid.uuid4()}.png"
    candidate = PurchaseCandidate(
        user_id=user_id,
        source_type=(
            PurchaseCandidateSource.SHARE_LINK.value if source_url else PurchaseCandidateSource.SCREENSHOT.value
        ),
        source_url=source_url,
        source_object_key=candidate_object_key,
        parsed_item={"category": Category.OUTERWEAR.value, "colors": ["camel"]},
    )
    session.add(candidate)
    await session.flush()

    if outfits == "auto":
        outfits = [
            {
                "slots": [
                    {"item_id": str(top_id), "slot": "torso"},
                    {"item_id": str(bottom_id), "slot": "legs"},
                ]
            }
        ]
    decision = WardrobeDecision(
        user_id=user_id,
        decision_type=WardrobeDecisionType.PURCHASE.value,
        candidate_id=candidate.id,
        context_snapshot={"concern": "volume"},
        evidence_snapshot={"outfit_evidence": {"outfits": outfits}},
        decision=PurchaseDecision.WORTH_ADDING.value,
        uncertainty=DecisionUncertainty.LOW.value,
        visual_need=DecisionVisualNeed.RECOMMENDED.value,
    )
    session.add(decision)
    await session.flush()

    # The wardrobe "moves on" after the decision: a NEWER item the snapshot
    # never mentioned. If the create endpoint recomposed from today's wardrobe
    # instead of replaying the snapshot, this item could reach the job payload
    # — the happy-path test asserts it never does (acceptance 1).
    await _seed_garment(session, user_id, category=Category.TOP.value)
    await session.commit()
    return decision.id


async def _post_preview(
    client: AsyncClient,
    decision_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    body: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> Any:
    headers = {"X-User-Id": str(user_id)}
    if correlation_id:
        headers["X-Correlation-Id"] = correlation_id
    return await client.post(
        f"/v1/wardrobe-decisions/{decision_id}/visual-preview",
        json=body if body is not None else FROZEN_BODY,
        headers=headers,
    )


# --- the frozen contract ------------------------------------------------------


async def test_the_frozen_body_yields_queued_and_replays_the_snapshot(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 1: the grounded outfit comes from the snapshot, not today's
    wardrobe — and the frozen §6.1 success shape is exactly two keys."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    decision_id = await _seed_decision(db_session, user_id)
    app = _build_app(db_session)

    async with _client(app) as client:
        resp = await _post_preview(client, decision_id, user_id)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert set(payload) == {"asset_id", "generation_status"}
    assert payload["generation_status"] == "queued"

    asset = (await db_session.scalars(select(OutfitVisualAsset))).one()
    job = (await db_session.scalars(select(AsyncJob))).one()
    decision = await db_session.get(WardrobeDecision, decision_id)
    assert decision is not None
    snapshot_slots = decision.evidence_snapshot["outfit_evidence"]["outfits"][0]["slots"]
    assert job.payload["garments"] == snapshot_slots
    # The consent attestation from the frozen body, persisted (contract §6.1 /
    # the V3-017 handoff: not covered by V3-013's avatar-level consent).
    assert asset.input_snapshot["consent_confirmed"] is True


async def test_a_second_post_returns_the_existing_asset(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §6.1: identical inputs never regenerate."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    decision_id = await _seed_decision(db_session, user_id)
    app = _build_app(db_session)

    async with _client(app) as client:
        first = await _post_preview(client, decision_id, user_id)
        second = await _post_preview(client, decision_id, user_id)

    assert first.json()["asset_id"] == second.json()["asset_id"]
    assert len((await db_session.scalars(select(AsyncJob))).all()) == 1


async def test_outfit_editor_state_cannot_be_smuggled_into_the_body(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 2, structurally: any field beyond the frozen three is 422,
    and nothing is written."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    decision_id = await _seed_decision(db_session, user_id)
    app = _build_app(db_session)

    async with _client(app) as client:
        resp = await _post_preview(
            client,
            decision_id,
            user_id,
            body={**FROZEN_BODY, "supporting_item_ids": [str(uuid.uuid4())]},
        )

    assert resp.status_code == 422
    assert (await db_session.scalars(select(OutfitVisualAsset))).first() is None


@pytest.mark.parametrize(
    "broken_body",
    [
        {**FROZEN_BODY, "consent": False},
        {"view_type": "static_tryon", "concern": "overall"},  # consent missing
        {**FROZEN_BODY, "concern": "redundancy"},  # §5.3's enum, not §6.1's
        {**FROZEN_BODY, "view_type": "outfit_multiview"},  # reserved for P1
    ],
)
async def test_out_of_vocabulary_bodies_are_validation_errors(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, broken_body: dict[str, Any]
) -> None:
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    decision_id = await _seed_decision(db_session, user_id)
    app = _build_app(db_session)

    async with _client(app) as client:
        resp = await _post_preview(client, decision_id, user_id, body=broken_body)

    assert resp.status_code == 422
    assert (await db_session.scalars(select(OutfitVisualAsset))).first() is None


# --- the refusal vocabulary ----------------------------------------------------


async def test_avatar_not_ready_is_4091_with_its_reason(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §6.1 verbatim: avatar not ready ⇒ 4091 STATE_CONFLICT."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    decision_id = await _seed_decision(db_session, user_id, with_avatar=False)
    app = _build_app(db_session)

    async with _client(app) as client:
        resp = await _post_preview(client, decision_id, user_id)

    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == 4091
    assert body["data"] == {"reason": "avatar_not_ready"}


async def test_a_link_candidate_is_no_preview_possible_not_an_error(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 4's BE half: the refusal is classified so FE can return to
    the decision evidence quietly (the V3-017 handoff, resolved here)."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    decision_id = await _seed_decision(
        db_session,
        user_id,
        candidate_object_key=None,
        source_url="https://shop.example.com/coat",
    )
    app = _build_app(db_session)

    async with _client(app) as client:
        resp = await _post_preview(client, decision_id, user_id)

    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == 4091
    assert body["data"] == {"reason": "no_renderable_candidate"}
    assert (await db_session.scalars(select(OutfitVisualAsset))).first() is None


async def test_a_snapshot_without_outfits_is_no_grounded_outfit(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    decision_id = await _seed_decision(db_session, user_id, outfits=[])
    app = _build_app(db_session)

    async with _client(app) as client:
        resp = await _post_preview(client, decision_id, user_id)

    assert resp.status_code == 409
    assert resp.json()["data"] == {"reason": "no_grounded_outfit"}


async def test_a_foreign_decision_is_indistinguishable_from_a_missing_one(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_flag(monkeypatch, enabled=True)
    owner_id = uuid.uuid4()
    decision_id = await _seed_decision(db_session, owner_id)
    stranger_id = uuid.uuid4()
    db_session.add(User(id=stranger_id))
    await db_session.commit()
    app = _build_app(db_session)

    async with _client(app) as client:
        foreign = await _post_preview(client, decision_id, stranger_id)
        missing = await _post_preview(client, uuid.uuid4(), stranger_id)

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["code"] == missing.json()["code"] == 4041


async def test_flag_off_answers_503_before_the_body_is_read(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_flag(monkeypatch, enabled=False)
    app = _build_app(db_session)

    async with _client(app) as client:
        resp = await client.post(
            f"/v1/wardrobe-decisions/{uuid.uuid4()}/visual-preview",
            json={"not": "the schema"},
            headers={"X-User-Id": str(uuid.uuid4())},
        )

    assert resp.status_code == 503
    assert resp.json()["code"] == 4003


# --- the polling lifecycle (acceptance 3, BE half) ------------------------------


@pytest.fixture
def worker_env(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> _FakeStorage:
    """The worker on its own engine (its sessions are module-level), plus the
    storage double — the test_v3_static_preview_jobs harness, minimally."""
    engine = create_async_engine(postgres_url, future=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(static_tryon_worker, "AsyncSessionLocal", maker)
    storage = _FakeStorage()
    monkeypatch.setattr(static_tryon_worker, "object_storage", lambda: storage)
    return storage


async def test_polling_reaches_ready_with_a_url_and_the_additive_timestamps(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, worker_env: _FakeStorage
) -> None:
    """POST → drain → GET is `ready`: `image_url` present, `created_at`
    populated, `expires_at` null (the §6.2 additive pair, stated honestly —
    no retention clock exists on the row in P0)."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    decision_id = await _seed_decision(db_session, user_id)
    app = _build_app(db_session)

    async with _client(app) as client:
        created = await _post_preview(client, decision_id, user_id)
        assert created.status_code == 200, created.text
        asset_id = created.json()["asset_id"]
        # The worker runs on its own engine, so the request's writes must be
        # committed to be claimable — in production get_db_session commits on
        # request exit; the test override hands back the raw session.
        await db_session.commit()

        drained = await static_tryon_worker.drain_static_tryon_jobs()
        assert drained == 1

        db_session.expire_all()
        polled = await client.get(f"/v1/outfit-visual-assets/{asset_id}", headers={"X-User-Id": str(user_id)})

    assert polled.status_code == 200
    body = polled.json()
    assert body["generation_status"] == "ready"
    assert body["image_url"]
    assert body["created_at"] is not None
    assert body["expires_at"] is None


async def test_a_rejected_render_polls_as_rejected_with_nothing_to_show(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, worker_env: _FakeStorage
) -> None:
    """The explicit quality fallback: `rejected_by_quality_gate` with
    `image_url` null AND `error_code` null — §6.2's frozen shape, which is what
    lets FE render it from the status alone with the same copy as `failed`."""
    _set_flag(monkeypatch, enabled=True)
    monkeypatch.setattr(static_tryon_worker, "visual_quality_gate", lambda: _RejectingGate())
    user_id = uuid.uuid4()
    decision_id = await _seed_decision(db_session, user_id)
    app = _build_app(db_session)

    async with _client(app) as client:
        created = await _post_preview(client, decision_id, user_id)
        asset_id = created.json()["asset_id"]
        await db_session.commit()

        drained = await static_tryon_worker.drain_static_tryon_jobs()
        assert drained == 1

        db_session.expire_all()
        polled = await client.get(f"/v1/outfit-visual-assets/{asset_id}", headers={"X-User-Id": str(user_id)})

    body = polled.json()
    assert body["generation_status"] == "rejected_by_quality_gate"
    assert body["image_url"] is None
    assert body["error_code"] is None


async def test_the_m10_get_stays_ungated_when_the_preview_flag_is_off(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, worker_env: _FakeStorage
) -> None:
    """Contract §2 rollback rule 4: cached assets remain readable via the
    ungated GET after the flag flips off — flag state never affects that GET."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    decision_id = await _seed_decision(db_session, user_id)
    app = _build_app(db_session)

    async with _client(app) as client:
        created = await _post_preview(client, decision_id, user_id)
        asset_id = created.json()["asset_id"]
        await static_tryon_worker.drain_static_tryon_jobs()

        _set_flag(monkeypatch, enabled=False)
        db_session.expire_all()
        gated = await _post_preview(client, decision_id, user_id)
        polled = await client.get(f"/v1/outfit-visual-assets/{asset_id}", headers={"X-User-Id": str(user_id)})

    assert gated.status_code == 503
    assert polled.status_code == 200
    assert polled.json()["generation_status"] == "ready"


# --- the cache-hit outcome event ------------------------------------------------


async def test_a_terminal_cache_hit_emits_the_outcome_it_served(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, worker_env: _FakeStorage
) -> None:
    """`v3_preview_outcome` with `cache_hit: true` — emitted by the router only
    when the served asset is already terminal and this request started nothing;
    the fresh enqueue emits nothing (its generation has not ended)."""
    _set_flag(monkeypatch, enabled=True)
    emitted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        visual_preview,
        "emit_v3_event",
        lambda name, fields, **kw: emitted.append((name, dict(fields))) or True,
    )
    user_id = uuid.uuid4()
    decision_id = await _seed_decision(db_session, user_id)
    app = _build_app(db_session)
    flow_id = str(uuid.uuid4())

    async with _client(app) as client:
        await _post_preview(client, decision_id, user_id, correlation_id=flow_id)
        assert emitted == []  # queued is not an outcome

        await static_tryon_worker.drain_static_tryon_jobs()
        db_session.expire_all()

        await _post_preview(client, decision_id, user_id, correlation_id=flow_id)

    assert emitted == [
        (
            "v3_preview_outcome",
            {"generation_status": "ready", "cache_hit": True, "retry_count": 0},
        )
    ]
