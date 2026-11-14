"""Tests for V4 M21: half-year / annual wardrobe report endpoint."""

import uuid
from datetime import UTC, date, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import ImageType
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitItem,
    WearHistory,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import insights as insights_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return Settings(APP_ENV="dev")

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(insights_router.router)
    return app


def enable_report(monkeypatch: pytest.MonkeyPatch, enabled: bool = True) -> None:
    """The router reads get_settings() directly (lru_cached), bypassing the dep
    override — so flag flips must patch the module-level get_settings. Matches
    the cost_guard / today_context pattern documented in CLAUDE.md."""

    def _patched() -> Settings:
        return Settings(APP_ENV="dev", ENABLE_WARDROBE_REPORT=enabled)

    monkeypatch.setattr(
        "app.presentation.api.v1.routers.insights.get_settings", _patched
    )


async def _make_outfit(
    session: AsyncSession,
    user_id: uuid.UUID,
    rec_date: date,
    title: str,
    item_ids: list[uuid.UUID],
    *,
    is_current: bool = True,
) -> Outfit:
    daily = DailyRecommendation(
        user_id=user_id,
        recommendation_date=rec_date,
        status="ready",
        generation_source="manual",
        is_current=is_current,
    )
    session.add(daily)
    await session.flush()

    outfit = Outfit(
        daily_recommendation_id=daily.id,
        user_id=user_id,
        outfit_type="safe",
        title=title,
        reason_text="Looks good",
        score_total=0.8,
        rank_order=1,
        created_at=datetime.now(UTC),
    )
    session.add(outfit)
    await session.flush()

    for order, item_id in enumerate(item_ids):
        session.add(
            OutfitItem(
                outfit_id=outfit.id,
                item_id=item_id,
                role="top" if order == 0 else "bottom",
                display_order=order,
                created_at=datetime.now(UTC),
            )
        )
    await session.flush()
    return outfit


async def _wear(
    session: AsyncSession, user_id: uuid.UUID, outfit_id: uuid.UUID, worn_date: date
) -> None:
    session.add(
        WearHistory(
            user_id=user_id,
            outfit_id=outfit_id,
            worn_date=worn_date,
            selected_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def seed_report_data(session: AsyncSession, user_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """One user, three items, two recommended outfits in H1; one worn twice."""
    session.add(User(id=user_id))
    await session.flush()

    shirt = WardrobeItem(
        user_id=user_id, title="White shirt", category="top", color_main="white",
        wear_count=8, style_tags=["casual", "minimal"],
        purchase_price=100, last_worn_at=date(2026, 6, 1),
    )
    pants = WardrobeItem(
        user_id=user_id, title="Black pants", category="bottom", color_main="black",
        wear_count=8, style_tags=["minimal"],
        purchase_price=200, last_worn_at=date(2026, 6, 1),
    )
    # Expensive, rarely worn → highest cost-per-wear (500/1 = 500).
    coat = WardrobeItem(
        user_id=user_id, title="Designer coat", category="outerwear", color_main="navy",
        wear_count=1, style_tags=["formal"],
        purchase_price=500, last_worn_at=date(2020, 1, 1),
    )
    for it in (shirt, pants, coat):
        session.add(it)
    await session.flush()

    # Combo A: shirt+pants, recommended + worn twice in H1.
    combo_a = await _make_outfit(
        session, user_id, date(2026, 3, 10), "Combo A", [shirt.id, pants.id]
    )
    await _wear(session, user_id, combo_a.id, date(2026, 3, 10))
    await _wear(session, user_id, combo_a.id, date(2026, 4, 12))

    # Combo B: coat-only, recommended but never worn (drags adoption to 1/2).
    await _make_outfit(session, user_id, date(2026, 3, 11), "Combo B", [coat.id])

    return {"shirt": shirt.id, "pants": pants.id, "coat": coat.id}


async def test_report_full(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    ids = await seed_report_data(db_session, user_id)
    enable_report(monkeypatch)
    app = build_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/insights/reports/2026-H1", headers={"X-User-Id": str(user_id)}
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["period"] == "2026-H1"
    assert data["is_empty"] is False
    # Combo A worn twice → 2 wear rows, each with 2 items.
    assert data["total_wears"] == 2

    # Top items: shirt + pants each worn twice in-period (not the lifetime coat).
    top_ids = {it["item_id"] for it in data["top_items"]}
    assert str(ids["shirt"]) in top_ids
    assert str(ids["pants"]) in top_ids
    assert str(ids["coat"]) not in top_ids
    assert all(it["wear_count"] == 2 for it in data["top_items"])

    # Most-worn combo.
    assert data["top_combos"][0]["title"] == "Combo A"
    assert data["top_combos"][0]["wear_count"] == 2

    # Color palette derives from worn items (white + black, no navy).
    assert data["color_palette"].get("white") == 2
    assert data["color_palette"].get("black") == 2
    assert "navy" not in data["color_palette"]

    # Highest cost-per-wear is the designer coat (500 / 1).
    assert data["highest_cost_per_wear"]["item_id"] == str(ids["coat"])
    assert float(data["highest_cost_per_wear"]["cost_per_wear"]) == 500.0

    # Longest idle = coat (last worn 2020).
    assert data["longest_idle_item"]["item_id"] == str(ids["coat"])

    # Adoption: 1 of 2 recommended outfits worn = 0.5.
    assert data["recommendation_adoption_rate"] == 0.5

    # Style keywords present.
    assert "minimal" in data["style_keywords"]


async def test_report_annual_period(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = uuid.uuid4()
    await seed_report_data(db_session, user_id)
    enable_report(monkeypatch)
    app = build_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/insights/reports/2026-annual", headers={"X-User-Id": str(user_id)}
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["period"] == "2026-annual"
    assert data["start_date"] == "2026-01-01"
    assert data["end_date"] == "2026-12-31"
    assert data["total_wears"] == 2


@pytest.mark.parametrize("period", ["2026-H1", "2026-annual", "2024-H1"])
async def test_report_signs_private_top_and_idle_covers(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, period: str
) -> None:
    user_id = uuid.uuid4()
    ids = await seed_report_data(db_session, user_id)
    enable_report(monkeypatch)
    settings = Settings(
        STORAGE_PROVIDER="s3",
        S3_BUCKET="aidrobe",
        S3_PUBLIC_BASE_URL="https://storage.example/aidrobe",
    )
    monkeypatch.setattr(
        "app.infrastructure.persistence.repositories.wardrobe_repo.get_settings", lambda: settings
    )
    calls: dict[str, int] = {}

    class PrivateStorage:
        def sign_render_url(self, key: str) -> str:
            calls[key] = calls.get(key, 0) + 1
            return f"https://signed.example/{key}?issue={calls[key]}"

    monkeypatch.setattr("app.bootstrap.container.object_storage", PrivateStorage)
    shirt = await db_session.get(WardrobeItem, ids["shirt"])
    pants = await db_session.get(WardrobeItem, ids["pants"])
    coat = await db_session.get(WardrobeItem, ids["coat"])
    assert shirt is not None and pants is not None and coat is not None
    standard_key = f"users/{user_id}/wardrobe/product/{shirt.id}.png"
    idle_key = f"users/{user_id}/wardrobe/upload/{coat.id}.jpg"
    shirt.cover_image_url = f"https://storage.example/aidrobe/{standard_key}?Expires=1"
    pants.cover_image_url = "https://shop.example/pants.jpg"
    coat.cover_image_url = f"https://storage.example/aidrobe/{idle_key}?Expires=1"
    # The selected/generated cover has a durable row. The idle card exercises
    # a URL-only legacy row, whose verified key can be recovered at read time.
    db_session.add(
        WardrobeItemImage(
            item_id=shirt.id,
            image_type=ImageType.STANDARD.value,
            image_url=shirt.cover_image_url,
            object_key=standard_key,
            created_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    app = build_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        for issuance in (1, 2):
            resp = await client.get(f"/v1/insights/reports/{period}", headers={"X-User-Id": str(user_id)})
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["longest_idle_item"]["cover_image_url"] == f"https://signed.example/{idle_key}?issue={issuance}"
            covers = {item["item_id"]: item["cover_image_url"] for item in data["top_items"]}
            if period == "2024-H1":
                assert not covers
            else:
                assert covers == {
                    str(shirt.id): f"https://signed.example/{standard_key}?issue={issuance}",
                    str(pants.id): "https://shop.example/pants.jpg",
                }

    assert calls[idle_key] == 2
    assert calls.get(standard_key, 0) == (0 if period == "2024-H1" else 2)
    assert shirt.cover_image_url == f"https://storage.example/aidrobe/{standard_key}?Expires=1"
    assert coat.cover_image_url == f"https://storage.example/aidrobe/{idle_key}?Expires=1"


async def test_report_empty_period(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A period before the user had any wears returns zeros, not an error."""
    user_id = uuid.uuid4()
    await seed_report_data(db_session, user_id)
    enable_report(monkeypatch)
    app = build_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/insights/reports/2024-H1", headers={"X-User-Id": str(user_id)}
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_empty"] is True
    assert data["total_wears"] == 0
    assert data["top_items"] == []
    assert data["top_combos"] == []
    assert data["color_palette"] == {}
    assert data["recommendation_adoption_rate"] == 0.0


@pytest.mark.parametrize("period", ["2026", "2026-H3", "26-H1", "twentytwentysix-H1", "2026-Q1", ""])
async def test_report_malformed_period_422(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, period: str
) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    enable_report(monkeypatch)
    app = build_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            f"/v1/insights/reports/{period}", headers={"X-User-Id": str(user_id)}
        )

    # Empty path segment collapses to /reports/ → 404 routing; the rest → 422.
    if period == "":
        assert resp.status_code in (404, 422)
    else:
        assert resp.status_code == 422


async def test_report_flag_off_returns_503(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = uuid.uuid4()
    await seed_report_data(db_session, user_id)
    enable_report(monkeypatch, enabled=False)
    app = build_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/insights/reports/2026-H1", headers={"X-User-Id": str(user_id)}
        )

    assert resp.status_code == 503
