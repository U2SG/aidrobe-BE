import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.recommendation import Outfit, OutfitFavorite, OutfitItem
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from tests.api.test_outfit_actions import build_outfit_app


async def seed_outfit(session: AsyncSession, user_id: uuid.UUID, title: str) -> Outfit:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    pieces = [
        WardrobeItem(user_id=user_id, title=f"{title} {role}", category=role, color_main="blue", status="wearable")
        for role in ("top", "bottom", "shoes")
    ]
    session.add_all(pieces)
    await session.flush()
    outfit = Outfit(
        user_id=user_id, outfit_type="stylish", title=title, reason_text="Saved design",
        score_total=Decimal("0.5"), rank_order=1, created_at=now,
        items=[
            OutfitItem(item_id=piece.id, role=piece.category, display_order=index, created_at=now)
            for index, piece in enumerate(pieces)
        ],
    )
    session.add(outfit)
    await session.flush()
    return outfit


async def test_favorites_list_preserves_saved_outfits_and_pages_with_stable_order(
    db_session: AsyncSession,
) -> None:
    user_id, other_user_id = uuid.uuid4(), uuid.uuid4()
    db_session.add_all([User(id=user_id), User(id=other_user_id)])
    await db_session.flush()
    first = await seed_outfit(db_session, user_id, "First design")
    second = await seed_outfit(db_session, user_id, "Second design")
    third = await seed_outfit(db_session, user_id, "Third design")
    unsaved = await seed_outfit(db_session, user_id, "Unsaved design")
    foreign = await seed_outfit(db_session, other_user_id, "Private design")
    now = datetime(2026, 2, 1, tzinfo=UTC)
    db_session.add_all([
        OutfitFavorite(user_id=user_id, outfit_id=first.id, created_at=now - timedelta(days=1)),
        OutfitFavorite(user_id=user_id, outfit_id=second.id, created_at=now),
        OutfitFavorite(user_id=user_id, outfit_id=third.id, created_at=now),
        # Both ownership predicates matter even if a stale row links another user's outfit.
        OutfitFavorite(user_id=user_id, outfit_id=foreign.id, created_at=now),
        OutfitFavorite(user_id=other_user_id, outfit_id=unsaved.id, created_at=now),
    ])
    await db_session.flush()
    expected = sorted([second, third], key=lambda outfit: outfit.id.int, reverse=True) + [first]
    app = build_outfit_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver", headers={"X-User-Id": str(user_id)}
    ) as client:
        first_page = await client.get("/v1/recommendations/outfits/favorites?limit=2")
        repeated_page = await client.get("/v1/recommendations/outfits/favorites?limit=2")
        second_page = await client.get("/v1/recommendations/outfits/favorites?limit=2&offset=2")
        past_end = await client.get("/v1/recommendations/outfits/favorites?limit=2&offset=3")
        detail = await client.get(f"/v1/recommendations/outfits/{first.id}")

    assert first_page.status_code == repeated_page.status_code == second_page.status_code == 200
    page = first_page.json()["data"]
    assert (page["limit"], page["offset"], page["has_more"]) == (2, 0, True)
    assert [item["outfit_id"] for item in page["items"]] == [str(outfit.id) for outfit in expected[:2]]
    assert repeated_page.json()["data"]["items"] == page["items"]
    last_page = second_page.json()["data"]
    assert (last_page["limit"], last_page["offset"], last_page["has_more"]) == (2, 2, False)
    saved = last_page["items"][0]
    assert saved["outfit_id"] == str(first.id)
    assert saved["title"] == "First design"
    assert saved["is_favorited"] is True
    assert [item["item_id"] for item in saved["items"]] == [str(item.item_id) for item in first.items]
    assert saved == detail.json()["data"]
    assert all(item["is_favorited"] for item in page["items"])
    assert past_end.json()["data"] == {"items": [], "limit": 2, "offset": 3, "has_more": False}


async def test_favorites_list_reflects_toggle_and_deleted_outfits(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    first = await seed_outfit(db_session, user_id, "Saved design")
    removed = await seed_outfit(db_session, user_id, "Deleted design")
    app = build_outfit_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver", headers={"X-User-Id": str(user_id)}
    ) as client:
        for outfit in (first, first, removed):
            response = await client.post(f"/v1/recommendations/outfits/{outfit.id}/favorite")
            assert response.status_code == 200
        saved = (await client.get("/v1/recommendations/outfits/favorites")).json()["data"]
        assert {item["outfit_id"] for item in saved["items"]} == {str(first.id), str(removed.id)}
        await db_session.delete(removed)
        await db_session.flush()
        remaining = (await client.get("/v1/recommendations/outfits/favorites")).json()["data"]
        assert [item["outfit_id"] for item in remaining["items"]] == [str(first.id)]
        assert (await client.delete(f"/v1/recommendations/outfits/{first.id}/favorite")).status_code == 200
        empty = (await client.get("/v1/recommendations/outfits/favorites")).json()["data"]
        assert empty == {"items": [], "limit": 20, "offset": 0, "has_more": False}


async def test_favorites_list_requires_authentication(db_session: AsyncSession) -> None:
    app = build_outfit_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        assert (await client.get("/v1/recommendations/outfits/favorites")).status_code == 401
        app.dependency_overrides[get_settings] = lambda: Settings(APP_ENV="prod")
        response = await client.get(
            "/v1/recommendations/outfits/favorites", headers={"X-User-Id": str(uuid.uuid4())}
        )
        assert response.status_code == 401


@pytest.mark.parametrize("query", ["limit=0", "limit=51", "offset=-1"])
async def test_favorites_list_rejects_invalid_pagination(db_session: AsyncSession, query: str) -> None:
    app = build_outfit_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get(
            f"/v1/recommendations/outfits/favorites?{query}", headers={"X-User-Id": str(uuid.uuid4())}
        )
    assert response.status_code == 422
