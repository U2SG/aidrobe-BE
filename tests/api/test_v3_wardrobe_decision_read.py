"""V3-008 — `GET /v1/wardrobe-decisions/{id}` (contract §5.3 replay).

What only this surface can show: that a decision read back later is the same
explanation the deciding POST produced — same wire shape, same copy, same
compared items — even after the wardrobe it described has changed, and that a
foreign decision is indistinguishable from a missing one. It also carries the
API-level half of V3-005's acceptance 3, deferred here because no response
schema existed before V3-007/V3-008: the §5.3 payload exposes no numeric
suitability score anywhere.

App-building convention copied from tests/api/test_v3_purchase_decision_endpoint.py:
the routers call `get_settings()` directly (lru-cached, so it bypasses
`app.dependency_overrides`), hence the flag is monkeypatched on the consuming
module.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import Category, ItemStatus, PurchaseCandidateSource, PurchaseCandidateStatus
from app.config.settings import Settings
from app.infrastructure.persistence.models.purchase_decision import PurchaseCandidate
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import purchase_candidates
from app.presentation.middlewares.exception_handler import setup_exception_handlers

PARSED_COAT: dict[str, Any] = {
    "category": Category.OUTERWEAR.value,
    "colors": ["gray"],
    "silhouette": None,
    "material_hints": ["wool"],
    "price": "¥899",
    "scene_tags": ["commute"],
}


def _build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def _db() -> AsyncSession:
        return session

    app.dependency_overrides[get_db_session] = _db
    app.include_router(purchase_candidates.router)
    app.include_router(purchase_candidates.wardrobe_decisions_router)
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _set_flag(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    monkeypatch.setattr(
        purchase_candidates,
        "get_settings",
        lambda: Settings(APP_ENV="dev", ENABLE_V3_PURCHASE_DECISION=enabled),
    )


async def _seed_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    session.add(User(id=user_id))
    await session.flush()


async def _add_item(
    session: AsyncSession, user_id: uuid.UUID, *, category: str, color: str = "black", title: str = "item"
) -> WardrobeItem:
    item = WardrobeItem(
        user_id=user_id,
        title=title,
        category=category,
        color_main=color,
        status=ItemStatus.WEARABLE.value,
    )
    session.add(item)
    await session.flush()
    return item


async def _seed_wardrobe(session: AsyncSession, user_id: uuid.UUID) -> WardrobeItem:
    await _add_item(session, user_id, category=Category.TOP.value, title="shirt")
    await _add_item(session, user_id, category=Category.BOTTOM.value, title="trousers")
    await _add_item(session, user_id, category=Category.SHOES.value, title="loafers")
    return await _add_item(
        session, user_id, category=Category.OUTERWEAR.value, color="gray", title="gray coat"
    )


async def _add_candidate(session: AsyncSession, user_id: uuid.UUID) -> PurchaseCandidate:
    candidate = PurchaseCandidate(
        user_id=user_id,
        source_type=PurchaseCandidateSource.SHARE_LINK.value,
        source_url="https://shop.example.com/gray-coat",
        status=PurchaseCandidateStatus.PARSED.value,
        parsed_item=dict(PARSED_COAT),
    )
    session.add(candidate)
    await session.flush()
    return candidate


async def _decide(client: AsyncClient, candidate_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, Any]:
    resp = await client.post(
        f"/v1/purchase-candidates/{candidate_id}/decision",
        headers={"X-User-Id": str(user_id)},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


# --- Replay fidelity --------------------------------------------------------


async def test_the_read_returns_exactly_what_the_deciding_post_returned(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One rendering path, two routes: the replay must be byte-equal to the
    decision the user was shown, or the stored explanation is a lie."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    await _seed_wardrobe(db_session, user_id)
    candidate = await _add_candidate(db_session, user_id)

    async with _client(_build_app(db_session)) as client:
        decided = await _decide(client, candidate.id, user_id)
        read = await client.get(
            f"/v1/wardrobe-decisions/{decided['id']}", headers={"X-User-Id": str(user_id)}
        )

    assert read.status_code == 200, read.text
    assert read.json() == decided


async def test_the_replay_survives_the_wardrobe_it_described_changing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The table exists so history cannot be rewritten: rename the compared
    coat and hard-delete a supporting item, and the explanation must not move.
    (`image_url` stays None throughout — no image rows are seeded — so the
    equality is over the snapshot-rendered fields, which is the point.)"""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    coat = await _seed_wardrobe(db_session, user_id)
    candidate = await _add_candidate(db_session, user_id)

    app = _build_app(db_session)
    async with _client(app) as client:
        decided = await _decide(client, candidate.id, user_id)

    coat.title = "renamed after the decision"
    await db_session.execute(
        delete(WardrobeItem).where(WardrobeItem.user_id == user_id, WardrobeItem.title == "shirt")
    )
    await db_session.flush()

    async with _client(app) as client:
        read = await client.get(
            f"/v1/wardrobe-decisions/{decided['id']}", headers={"X-User-Id": str(user_id)}
        )

    assert read.status_code == 200, read.text
    assert read.json() == decided


# --- V3-005 acceptance 3, at the API layer ----------------------------------


async def test_the_payload_exposes_no_numeric_score_anywhere(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frozen §5.3 shape has no numeric field at all — no similarity
    percentage, no count, no score. V3-005 asserted this on the evidence shape
    and deferred the API half to V3-008; this is that assertion. Bools are
    checked before ints because `isinstance(True, int)` is True in Python."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    await _seed_wardrobe(db_session, user_id)
    candidate = await _add_candidate(db_session, user_id)

    async with _client(_build_app(db_session)) as client:
        decided = await _decide(client, candidate.id, user_id)
        read = await client.get(
            f"/v1/wardrobe-decisions/{decided['id']}", headers={"X-User-Id": str(user_id)}
        )

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, bool):
            return
        assert not isinstance(node, int | float), f"numeric value at {path}: {node!r}"
        if isinstance(node, dict):
            for key, value in node.items():
                _walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                _walk(value, f"{path}[{index}]")

    _walk(read.json(), "$")


# --- Ownership and error states ---------------------------------------------


async def test_a_foreign_decision_is_indistinguishable_from_a_missing_one(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §3: no existence leak — same status, same body."""
    _set_flag(monkeypatch, enabled=True)
    owner_id, stranger_id = uuid.uuid4(), uuid.uuid4()
    await _seed_user(db_session, owner_id)
    await _seed_user(db_session, stranger_id)
    await _seed_wardrobe(db_session, owner_id)
    candidate = await _add_candidate(db_session, owner_id)

    async with _client(_build_app(db_session)) as client:
        decided = await _decide(client, candidate.id, owner_id)
        foreign = await client.get(
            f"/v1/wardrobe-decisions/{decided['id']}", headers={"X-User-Id": str(stranger_id)}
        )
        missing = await client.get(
            f"/v1/wardrobe-decisions/{uuid.uuid4()}", headers={"X-User-Id": str(stranger_id)}
        )

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


async def test_the_flag_hides_the_replay_and_restores_it_byte_identically(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §2 rule 3 for the read surface: off → 503 (hidden, not lost),
    back on → the identical explanation."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    await _seed_wardrobe(db_session, user_id)
    candidate = await _add_candidate(db_session, user_id)

    async with _client(_build_app(db_session)) as client:
        decided = await _decide(client, candidate.id, user_id)
        first_read = await client.get(
            f"/v1/wardrobe-decisions/{decided['id']}", headers={"X-User-Id": str(user_id)}
        )
    assert first_read.status_code == 200

    _set_flag(monkeypatch, enabled=False)
    async with _client(_build_app(db_session)) as client:
        hidden = await client.get(
            f"/v1/wardrobe-decisions/{decided['id']}", headers={"X-User-Id": str(user_id)}
        )
    assert hidden.status_code == 503

    _set_flag(monkeypatch, enabled=True)
    async with _client(_build_app(db_session)) as client:
        restored = await client.get(
            f"/v1/wardrobe-decisions/{decided['id']}", headers={"X-User-Id": str(user_id)}
        )
    assert restored.status_code == 200
    assert restored.json() == first_read.json()
