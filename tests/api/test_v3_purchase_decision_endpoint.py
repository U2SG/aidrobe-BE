"""V3-007 — `POST /v1/purchase-candidates/{id}/decision` (contract §5.3).

The wire shape, the ownership rules and the flag rollback, at the HTTP layer.
The decision matrix itself is `tests/unit/test_v3_purchase_decision_rules.py`
and the persistence/idempotency behaviour is
`tests/services/test_v3_purchase_decision.py`; this file asserts what only a
request can show — that the frozen §5.3 keys are all present, that a foreign
candidate is indistinguishable from a missing one, that thumbnails are signed
at read time rather than served from the snapshot, and that flipping the flag
off hides an evaluation that already exists.

App-building convention copied from tests/api/test_v3_purchase_candidates.py:
the router calls `get_settings()` directly (lru-cached, so it bypasses
`app.dependency_overrides`), hence the flag is monkeypatched on the consuming
module.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.purchase.purchase_decision_rules import (
    MAX_REASONS,
    MIN_REASONS,
    NEXT_ACTIONS,
    REASON_KINDS,
)
from app.common.enums import (
    Category,
    DecisionUncertainty,
    DecisionVisualNeed,
    ImageType,
    ItemStatus,
    PurchaseCandidateSource,
    PurchaseCandidateStatus,
    PurchaseDecision,
)
from app.config.settings import Settings
from app.infrastructure.persistence.models.purchase_decision import (
    PurchaseCandidate,
    WardrobeDecision,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
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
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    category: str,
    color: str = "black",
    title: str = "item",
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


async def _add_candidate(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    parsed: Any = PARSED_COAT,
    status: str = PurchaseCandidateStatus.PARSED.value,
) -> PurchaseCandidate:
    candidate = PurchaseCandidate(
        user_id=user_id,
        source_type=PurchaseCandidateSource.SHARE_LINK.value,
        source_url="https://shop.example.com/gray-coat",
        status=status,
        parsed_item=dict(parsed) if isinstance(parsed, dict) else parsed,
    )
    session.add(candidate)
    await session.flush()
    return candidate


async def _wearable_pool(session: AsyncSession, user_id: uuid.UUID) -> None:
    await _add_item(session, user_id, category=Category.TOP.value, title="shirt")
    await _add_item(session, user_id, category=Category.BOTTOM.value, title="trousers")
    await _add_item(session, user_id, category=Category.SHOES.value, title="loafers")


# --- The frozen §5.3 response shape --------------------------------------


async def test_a_decision_returns_every_frozen_field(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    await _wearable_pool(db_session, user_id)
    candidate = await _add_candidate(db_session, user_id)

    async with _client(_build_app(db_session)) as client:
        resp = await client.post(
            f"/v1/purchase-candidates/{candidate.id}/decision",
            headers={"X-User-Id": str(user_id)},
            json={"concern": "volume"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "id",
        "candidate_id",
        "decision",
        "decision_text",
        "reasons",
        "closet_comparison",
        "uncertainty",
        "visual_need",
        "boundary_note",
        "next_actions",
        "created_at",
    }
    assert body["candidate_id"] == str(candidate.id)
    assert body["decision"] in {d.value for d in PurchaseDecision}
    assert body["uncertainty"] in {u.value for u in DecisionUncertainty}
    assert body["visual_need"] in {v.value for v in DecisionVisualNeed}
    assert MIN_REASONS <= len(body["reasons"]) <= MAX_REASONS
    assert {r["kind"] for r in body["reasons"]} <= REASON_KINDS
    assert all(r["text"].strip() for r in body["reasons"])
    assert set(body["next_actions"]) <= NEXT_ACTIONS
    assert body["decision_text"].strip() and body["boundary_note"].strip()
    assert set(body["closet_comparison"]) == {"summary_text", "similar_items"}


async def test_an_omitted_body_is_treated_as_no_concern(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The body is optional (contract §5.3), so a client with no concern to
    declare must not have to send one."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    await _wearable_pool(db_session, user_id)
    candidate = await _add_candidate(db_session, user_id)

    async with _client(_build_app(db_session)) as client:
        resp = await client.post(
            f"/v1/purchase-candidates/{candidate.id}/decision",
            headers={"X-User-Id": str(user_id)},
        )

    assert resp.status_code == 200, resp.text
    stored = await db_session.scalar(
        select(WardrobeDecision).where(WardrobeDecision.candidate_id == candidate.id)
    )
    assert stored is not None
    assert stored.context_snapshot["concern"] == "none"


async def test_an_unknown_concern_is_rejected_by_validation(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frozen enum is the request contract; the rules layer tolerates a
    stray value, but the wire must not quietly accept one."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    candidate = await _add_candidate(db_session, user_id)

    async with _client(_build_app(db_session)) as client:
        resp = await client.post(
            f"/v1/purchase-candidates/{candidate.id}/decision",
            headers={"X-User-Id": str(user_id)},
            json={"concern": "vibes"},
        )

    assert resp.status_code == 422


async def test_a_repeated_call_returns_the_same_decision_id(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    await _wearable_pool(db_session, user_id)
    candidate = await _add_candidate(db_session, user_id)

    async with _client(_build_app(db_session)) as client:
        first = await client.post(
            f"/v1/purchase-candidates/{candidate.id}/decision",
            headers={"X-User-Id": str(user_id)},
        )
        second = await client.post(
            f"/v1/purchase-candidates/{candidate.id}/decision",
            headers={"X-User-Id": str(user_id)},
            json={"concern": "color"},
        )

    assert first.json()["id"] == second.json()["id"]
    assert first.json() == second.json()


# --- Ownership and error states ------------------------------------------


async def test_a_foreign_candidate_is_not_found_rather_than_forbidden(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §3: a resource that exists but is not the caller's must not be
    distinguishable from one that does not exist."""
    _set_flag(monkeypatch, enabled=True)
    owner_id, stranger_id = uuid.uuid4(), uuid.uuid4()
    await _seed_user(db_session, owner_id)
    await _seed_user(db_session, stranger_id)
    candidate = await _add_candidate(db_session, owner_id)

    async with _client(_build_app(db_session)) as client:
        foreign = await client.post(
            f"/v1/purchase-candidates/{candidate.id}/decision",
            headers={"X-User-Id": str(stranger_id)},
        )
        missing = await client.post(
            f"/v1/purchase-candidates/{uuid.uuid4()}/decision",
            headers={"X-User-Id": str(stranger_id)},
        )

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    # And nothing was written for the stranger.
    assert not list(
        await db_session.scalars(
            select(WardrobeDecision).where(WardrobeDecision.user_id == stranger_id)
        )
    )


async def test_an_unparsed_candidate_answers_insufficient_information(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    await _wearable_pool(db_session, user_id)
    candidate = await _add_candidate(
        db_session, user_id, parsed=None, status=PurchaseCandidateStatus.PARSE_FAILED.value
    )

    async with _client(_build_app(db_session)) as client:
        resp = await client.post(
            f"/v1/purchase-candidates/{candidate.id}/decision",
            headers={"X-User-Id": str(user_id)},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision"] == PurchaseDecision.INSUFFICIENT_INFORMATION.value
    assert body["visual_need"] == DecisionVisualNeed.NONE.value
    assert body["closet_comparison"]["similar_items"] == []


# --- Read-time signing ----------------------------------------------------


async def test_thumbnails_are_signed_at_read_time_and_never_snapshotted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`image_url` is a 30-minute URL (V3_SPEC §13.5): it must appear in the
    response and must not exist anywhere in the stored explanation."""
    _set_flag(monkeypatch, enabled=True)
    signed: list[str] = []

    class _Storage:
        def sign_render_url(self, object_key: str, *args: Any, **kwargs: Any) -> str:
            signed.append(object_key)
            return f"https://signed.example.com/{object_key}?token=stub"

    monkeypatch.setattr("app.bootstrap.container.object_storage", lambda: _Storage())
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    await _wearable_pool(db_session, user_id)
    owned = await _add_item(
        db_session, user_id, category=Category.OUTERWEAR.value, color="gray", title="gray coat"
    )
    db_session.add(
        WardrobeItemImage(
            item_id=owned.id,
            image_type=ImageType.STANDARD.value,
            image_url="https://cdn.example.com/original-coat.jpg",
            object_key=f"users/{user_id}/coat.jpg",
            created_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    candidate = await _add_candidate(db_session, user_id)

    async with _client(_build_app(db_session)) as client:
        resp = await client.post(
            f"/v1/purchase-candidates/{candidate.id}/decision",
            headers={"X-User-Id": str(user_id)},
        )

    body = resp.json()
    compared = body["closet_comparison"]["similar_items"]
    assert compared, "the duplicate coat should be compared"
    assert compared[0]["item_id"] == str(owned.id)
    assert compared[0]["image_url"] == f"https://signed.example.com/users/{user_id}/coat.jpg?token=stub"
    assert signed == [f"users/{user_id}/coat.jpg"]

    stored = await db_session.scalar(
        select(WardrobeDecision).where(WardrobeDecision.candidate_id == candidate.id)
    )
    assert stored is not None
    assert "signed.example.com" not in str(stored.evidence_snapshot)


async def test_an_item_without_an_image_still_renders(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing thumbnail is a display detail, never an error."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    await _wearable_pool(db_session, user_id)
    await _add_item(db_session, user_id, category=Category.OUTERWEAR.value, color="gray")
    candidate = await _add_candidate(db_session, user_id)

    async with _client(_build_app(db_session)) as client:
        resp = await client.post(
            f"/v1/purchase-candidates/{candidate.id}/decision",
            headers={"X-User-Id": str(user_id)},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["closet_comparison"]["similar_items"][0]["image_url"] is None


# --- Acceptance 4: the flag hides the evaluation --------------------------


async def test_the_flag_hides_an_evaluation_that_already_exists(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback is not just "no new decisions": a decision written while the
    flag was on must stop being reachable when it goes off — and must still be
    in the database, so flipping it back is lossless (contract §2 rule 3).
    """
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    await _wearable_pool(db_session, user_id)
    candidate = await _add_candidate(db_session, user_id)

    async with _client(_build_app(db_session)) as client:
        created = await client.post(
            f"/v1/purchase-candidates/{candidate.id}/decision",
            headers={"X-User-Id": str(user_id)},
        )
    assert created.status_code == 200

    _set_flag(monkeypatch, enabled=False)
    async with _client(_build_app(db_session)) as client:
        hidden = await client.post(
            f"/v1/purchase-candidates/{candidate.id}/decision",
            headers={"X-User-Id": str(user_id)},
        )

    assert hidden.status_code == 503
    row = await db_session.scalar(
        select(WardrobeDecision).where(WardrobeDecision.candidate_id == candidate.id)
    )
    assert row is not None

    _set_flag(monkeypatch, enabled=True)
    async with _client(_build_app(db_session)) as client:
        restored = await client.post(
            f"/v1/purchase-candidates/{candidate.id}/decision",
            headers={"X-User-Id": str(user_id)},
        )

    assert restored.status_code == 200
    assert restored.json() == created.json()


async def test_the_flag_off_response_does_not_inspect_the_body(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """503 must win over 422: while the flag is off the surface does not exist,
    so an invalid body must not leak its schema."""
    _set_flag(monkeypatch, enabled=False)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)

    async with _client(_build_app(db_session)) as client:
        resp = await client.post(
            f"/v1/purchase-candidates/{uuid.uuid4()}/decision",
            headers={"X-User-Id": str(user_id)},
            json={"concern": "vibes"},
        )

    assert resp.status_code == 503
