"""Candidate retries preserve the original parse, including concurrent requests."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.ports.vision import VisionResult
from app.application.purchase.ingest_candidate import IngestPurchaseCandidate
from app.common import error_codes
from app.common.locale import DEFAULT_LOCALE
from app.infrastructure.ocr.fake_ocr import FakeOcr
from app.infrastructure.persistence.models.purchase_decision import PurchaseCandidate
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.purchase_decision_repo import (
    SqlAlchemyPurchaseCandidateRepository,
)
from app.infrastructure.vision.fake_vision import FakeVision
from app.presentation.api.v1.routers import purchase_candidates
from tests.api.test_v3_purchase_candidates import (
    _StubLinkParser,
    _build_app,
    _client,
    _enable_flag,
    _key_for,
    _seed_user,
    _signer,
)


class _CountingVision(FakeVision):
    def __init__(self, *, delay: float = 0) -> None:
        self.calls = 0
        self.delay = delay

    async def analyze_image(
        self, image_url: str, locale: str = DEFAULT_LOCALE, *, source_key: str | None = None,
    ) -> VisionResult:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return await super().analyze_image(image_url, locale, source_key=source_key)


@pytest.mark.parametrize("filename,status", [("pants.jpg", "parsed"), ("fail.jpg", "parse_failed")])
async def test_retry_returns_same_terminal_candidate_without_another_provider_call(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, filename: str, status: str,
) -> None:
    user_id, request_id = uuid.uuid4(), uuid.uuid4()
    await _seed_user(db_session, user_id)
    _enable_flag(monkeypatch)
    app, _ = _build_app(db_session)
    vision = _CountingVision()
    app.dependency_overrides[purchase_candidates.get_vision_service] = lambda: vision
    payload = {
        "source_type": "screenshot", "source_asset_id": _key_for(user_id, filename), "request_id": str(request_id),
    }
    async with _client(app) as client:
        first = await client.post("/v1/purchase-candidates", headers={"X-User-Id": str(user_id)}, json=payload)
        retry = await client.post("/v1/purchase-candidates", headers={"X-User-Id": str(user_id)}, json=payload)
    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert first.json()["status"] == status
    assert vision.calls == 1
    rows = list(await db_session.scalars(select(PurchaseCandidate).where(PurchaseCandidate.user_id == user_id)))
    assert len(rows) == 1
    assert rows[0].client_request_id == request_id


@pytest.mark.parametrize("change", ["source_type", "source_asset_id", "source_url"])
async def test_reusing_request_id_for_another_source_conflicts_before_parsing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    _enable_flag(monkeypatch)
    app, links = _build_app(db_session)
    vision = _CountingVision()
    app.dependency_overrides[purchase_candidates.get_vision_service] = lambda: vision
    payload = {
        "source_type": "screenshot", "source_asset_id": _key_for(user_id), "request_id": str(uuid.uuid4()),
    }
    changed = dict(payload)
    if change == "source_type":
        changed[change] = "order_image"
    elif change == "source_asset_id":
        changed[change] = _key_for(user_id, "another.jpg")
    else:
        changed = {
            "source_type": "ask_link", "source_url": "https://shop.example.com/new",
            "request_id": payload["request_id"],
        }
    async with _client(app) as client:
        first = await client.post("/v1/purchase-candidates", headers={"X-User-Id": str(user_id)}, json=payload)
        conflict = await client.post("/v1/purchase-candidates", headers={"X-User-Id": str(user_id)}, json=changed)
    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["code"] == error_codes.STATE_CONFLICT
    assert vision.calls == 1
    assert links.calls == []


async def test_link_replay_skips_parser_and_changed_url_conflicts(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    _enable_flag(monkeypatch)
    app, links = _build_app(db_session)
    payload = {
        "source_type": "ask_link", "source_url": "https://shop.example.com/coat", "request_id": str(uuid.uuid4()),
    }
    async with _client(app) as client:
        first = await client.post("/v1/purchase-candidates", headers={"X-User-Id": str(user_id)}, json=payload)
        retry = await client.post("/v1/purchase-candidates", headers={"X-User-Id": str(user_id)}, json=payload)
        changed = await client.post("/v1/purchase-candidates", headers={"X-User-Id": str(user_id)}, json={
            **payload, "source_url": "https://shop.example.com/trousers",
        })
    assert first.status_code == retry.status_code == 201
    assert first.json() == retry.json()
    assert changed.status_code == 409
    assert links.calls == [payload["source_url"]]


async def test_request_id_is_user_scoped_and_replay_cannot_bypass_upload_ownership(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, other = uuid.uuid4(), uuid.uuid4()
    for user_id in (owner, other):
        await _seed_user(db_session, user_id)
    _enable_flag(monkeypatch)
    app, _ = _build_app(db_session)
    request_id = str(uuid.uuid4())
    async with _client(app) as client:
        first = await client.post("/v1/purchase-candidates", headers={"X-User-Id": str(owner)}, json={
            "source_type": "screenshot", "source_asset_id": _key_for(owner), "request_id": request_id,
        })
        second = await client.post("/v1/purchase-candidates", headers={"X-User-Id": str(other)}, json={
            "source_type": "screenshot", "source_asset_id": _key_for(other), "request_id": request_id,
        })
        foreign = await client.post("/v1/purchase-candidates", headers={"X-User-Id": str(other)}, json={
            "source_type": "screenshot", "source_asset_id": _key_for(owner), "request_id": request_id,
        })
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert foreign.status_code == 404


async def test_omitting_request_id_preserves_independent_candidate_creation(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    _enable_flag(monkeypatch)
    app, _ = _build_app(db_session)
    payload = {"source_type": "screenshot", "source_asset_id": _key_for(user_id)}
    async with _client(app) as client:
        first = await client.post("/v1/purchase-candidates", headers={"X-User-Id": str(user_id)}, json=payload)
        second = await client.post("/v1/purchase-candidates", headers={"X-User-Id": str(user_id)}, json=payload)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


async def test_concurrent_retries_use_one_candidate_and_one_provider_call(postgres_url: str) -> None:
    engine = create_async_engine(postgres_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id, request_id = uuid.uuid4(), uuid.uuid4()
    vision = _CountingVision(delay=0.1)

    async def ingest() -> uuid.UUID:
        # Separate sessions/transactions are essential: a shared db_session
        # would hide the duplicate-parse race behind one connection.
        async with factory() as session:
            result = await IngestPurchaseCandidate(
                SqlAlchemyPurchaseCandidateRepository(session), vision, FakeOcr(),
                _StubLinkParser(), signer=_signer,
            ).execute(
                user_id, source_type="screenshot", source_object_key=_key_for(user_id), request_id=request_id,
            )
            await session.commit()
            return result.id

    try:
        async with factory() as session:
            session.add(User(id=user_id))
            await session.commit()
        first, retry = await asyncio.wait_for(asyncio.gather(ingest(), ingest()), timeout=10)
        assert first == retry
        assert vision.calls == 1
        async with factory() as session:
            rows = list(await session.scalars(
                select(PurchaseCandidate).where(PurchaseCandidate.user_id == user_id),
            ))
            assert len(rows) == 1
    finally:
        await engine.dispose()
