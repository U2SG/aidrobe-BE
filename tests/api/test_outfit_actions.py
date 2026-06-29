import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import (
    ImageType,
    ItemStatus,
    RecommendationEventType,
    RecommendationStatus,
)
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.fit import FitFeedback
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitFavorite,
    OutfitFeedback,
    OutfitItem,
    RecommendationEvent,
    WearHistory,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import recommendations as recommendations_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers
from tests.api.test_recommendations_today import seed_ready_recommendation


def build_outfit_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return Settings(APP_ENV="dev")

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(recommendations_router.router)
    return app


async def get_first_outfit_id(client: AsyncClient, user_id: uuid.UUID) -> str:
    response = await client.get("/v1/recommendations/today", headers={"X-User-Id": str(user_id)})
    return response.json()["data"]["outfits"][0]["outfit_id"]


async def seed_outfit_with_replacement_candidates(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    session.add(User(id=user_id))
    await session.flush()
    now = datetime.now(UTC)
    target = WardrobeItem(
        user_id=user_id,
        title="White shirt",
        category="top",
        color_main="white",
        status=ItemStatus.WEARABLE.value,
    )
    bottom = WardrobeItem(
        user_id=user_id,
        title="Black pants",
        category="bottom",
        color_main="black",
        status=ItemStatus.WEARABLE.value,
    )
    candidate = WardrobeItem(
        user_id=user_id,
        title="Cream shirt",
        category="top",
        color_main="white",
        status=ItemStatus.WEARABLE.value,
    )
    other_category = WardrobeItem(
        user_id=user_id,
        title="White shoes",
        category="shoes",
        color_main="white",
        status=ItemStatus.WEARABLE.value,
    )
    foreign_user_id = uuid.uuid4()
    foreign_candidate = WardrobeItem(
        user_id=foreign_user_id,
        title="Foreign shirt",
        category="top",
        color_main="white",
        status=ItemStatus.WEARABLE.value,
    )
    session.add(User(id=foreign_user_id))
    session.add_all([target, bottom, candidate, other_category, foreign_candidate])
    await session.flush()
    session.add(
        WardrobeItemImage(
            item_id=candidate.id,
            image_type=ImageType.STANDARD.value,
            image_url="https://cdn.example/cream-shirt.jpg",
            object_key=f"users/{user_id}/items/{candidate.id}/standard.png",
            created_at=now,
        )
    )
    daily = DailyRecommendation(
        user_id=user_id,
        recommendation_date=date(2026, 6, 26),
        status=RecommendationStatus.READY.value,
        generation_source="test",
        score_breakdown=None,
    )
    session.add(daily)
    await session.flush()
    outfit = Outfit(
        daily_recommendation_id=daily.id,
        user_id=user_id,
        outfit_type="safe",
        title="Test outfit",
        reason_text="Seeded for replacement tests",
        score_total=Decimal("1.0"),
        rank_order=1,
        created_at=now,
    )
    session.add(outfit)
    await session.flush()
    session.add_all(
        [
            OutfitItem(
                outfit_id=outfit.id,
                item_id=target.id,
                role="top",
                display_order=1,
                created_at=now,
            ),
            OutfitItem(
                outfit_id=outfit.id,
                item_id=bottom.id,
                role="bottom",
                display_order=2,
                created_at=now,
            ),
        ]
    )
    await session.flush()
    return outfit.id, target.id, candidate.id


async def test_get_outfit_detail_returns_items(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        response = await client.get(
            f"/v1/recommendations/outfits/{outfit_id}",
            headers={"X-User-Id": str(user_id)},
        )

    assert response.status_code == 200
    assert response.json()["data"]["items"]


async def test_replacement_candidates_are_same_category_owned_and_signed(
    db_session: AsyncSession,
) -> None:
    user_id = uuid.uuid4()
    outfit_id, target_id, candidate_id = await seed_outfit_with_replacement_candidates(
        db_session, user_id
    )
    app = build_outfit_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/v1/recommendations/outfits/{outfit_id}/items/{target_id}/replacements",
            headers={"X-User-Id": str(user_id)},
        )

    assert response.status_code == 200
    replacements = response.json()["data"]["replacements"]
    assert [item["item_id"] for item in replacements] == [str(candidate_id)]
    thumbnail_url = (
        f"http://localhost:9000/local/files/users/{user_id}/items/"
        f"{candidate_id}/standard.png"
    )
    assert replacements[0] == {
        "item_id": str(candidate_id),
        "title": "Cream shirt",
        "category": "top",
        "color_main": "white",
        "thumbnail_url": thumbnail_url,
        "similarity": 0.9,
    }


async def test_replacement_candidates_reject_foreign_outfit(
    db_session: AsyncSession,
) -> None:
    user_id = uuid.uuid4()
    outfit_id, target_id, _candidate_id = await seed_outfit_with_replacement_candidates(
        db_session, user_id
    )
    app = build_outfit_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/v1/recommendations/outfits/{outfit_id}/items/{target_id}/replacements",
            headers={"X-User-Id": str(uuid.uuid4())},
        )

    assert response.status_code == 404


async def test_favorite_toggle_is_idempotent_and_reflected_in_reads(
    db_session: AsyncSession,
) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        first = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/favorite",
            headers={"X-User-Id": str(user_id)},
        )
        second = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/favorite",
            headers={"X-User-Id": str(user_id)},
        )
        detail = await client.get(
            f"/v1/recommendations/outfits/{outfit_id}",
            headers={"X-User-Id": str(user_id)},
        )
        today = await client.get(
            "/v1/recommendations/today",
            headers={"X-User-Id": str(user_id)},
        )
        delete_first = await client.delete(
            f"/v1/recommendations/outfits/{outfit_id}/favorite",
            headers={"X-User-Id": str(user_id)},
        )
        delete_second = await client.delete(
            f"/v1/recommendations/outfits/{outfit_id}/favorite",
            headers={"X-User-Id": str(user_id)},
        )
        detail_after_delete = await client.get(
            f"/v1/recommendations/outfits/{outfit_id}",
            headers={"X-User-Id": str(user_id)},
        )

    count = await db_session.scalar(select(func.count()).select_from(OutfitFavorite))
    assert first.status_code == 200
    assert second.status_code == 200
    assert detail.json()["data"]["is_favorited"] is True
    today_outfits = today.json()["data"]["outfits"]
    assert any(
        outfit["outfit_id"] == outfit_id and outfit["is_favorited"]
        for outfit in today_outfits
    )
    assert delete_first.status_code == 200
    assert delete_second.status_code == 200
    assert detail_after_delete.json()["data"]["is_favorited"] is False
    assert count == 0


async def test_wear_outfit_is_idempotent_for_same_date(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        payload = {"worn_date": "2026-05-01"}
        first = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/wear",
            headers={"X-User-Id": str(user_id)},
            json=payload,
        )
        second = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/wear",
            headers={"X-User-Id": str(user_id)},
            json=payload,
        )

    count = await db_session.scalar(select(func.count()).select_from(WearHistory))
    assert first.status_code == 200
    assert second.status_code == 200
    assert count == 1


async def test_feedback_accepts_like_and_rejects_hate(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        response = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/feedback",
            headers={"X-User-Id": str(user_id)},
            json={"feedback_type": "like"},
        )
        invalid = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/feedback",
            headers={"X-User-Id": str(user_id)},
            json={"feedback_type": "hate"},
        )

    count = await db_session.scalar(select(func.count()).select_from(OutfitFeedback))
    assert response.status_code == 200
    assert invalid.status_code == 422
    assert count == 1


@pytest.mark.parametrize("feedback_type", ["love", "neutral"])
async def test_feedback_accepts_new_four_tier_values(
    db_session: AsyncSession, feedback_type: str
) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        response = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/feedback",
            headers={"X-User-Id": str(user_id)},
            json={"feedback_type": feedback_type},
        )

    feedback = await db_session.scalar(
        select(OutfitFeedback).where(OutfitFeedback.outfit_id == uuid.UUID(outfit_id))
    )
    assert response.status_code == 200
    assert feedback is not None
    assert feedback.feedback_type == feedback_type


async def test_feedback_emits_attributed_recommendation_event(db_session: AsyncSession) -> None:
    # BE#40 — a polar rating also lands an attributed interaction event.
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        response = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/feedback",
            headers={"X-User-Id": str(user_id)},
            json={"feedback_type": "love", "reason_tags": ["sharp"]},
        )

    assert response.status_code == 200
    event = await db_session.scalar(
        select(RecommendationEvent).where(RecommendationEvent.outfit_id == uuid.UUID(outfit_id))
    )
    assert event is not None
    assert event.event_type == RecommendationEventType.LIKED.value
    assert event.user_id == user_id
    assert event.recommendation_id is not None  # attributed to the daily
    assert event.source == "outfit_feedback"
    assert event.event_metadata["feedback_type"] == "love"
    assert event.event_metadata["reason_tags"] == ["sharp"]
    # BE#67 — the outfit's item-set signature rides along for feedback learning.
    assert isinstance(event.event_metadata["item_ids"], list)
    assert event.event_metadata["item_ids"]


async def test_neutral_feedback_emits_neutral_event(db_session: AsyncSession) -> None:
    # Every rating is a signal — neutral emits a `neutral` event (raw rating in
    # event_metadata) so a later like/dislike doesn't erase it from the stream.
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/feedback",
            headers={"X-User-Id": str(user_id)},
            json={"feedback_type": "neutral"},
        )

    event = await db_session.scalar(select(RecommendationEvent))
    assert event is not None
    assert event.event_type == RecommendationEventType.NEUTRAL.value
    assert event.event_metadata["feedback_type"] == "neutral"
    assert event.event_metadata["reason_tags"] == []
    assert isinstance(event.event_metadata["item_ids"], list)  # BE#67 signature


async def test_wear_emits_single_worn_event_idempotent(db_session: AsyncSession) -> None:
    # BE#40 — a real wear emits one WORN event; a duplicate wear (same day) is
    # idempotent and must NOT add a second.
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        for _ in range(2):
            await client.post(
                f"/v1/recommendations/outfits/{outfit_id}/wear",
                headers={"X-User-Id": str(user_id)},
                json={"worn_date": "2026-06-24"},
            )

    count = await db_session.scalar(
        select(func.count())
        .select_from(RecommendationEvent)
        .where(RecommendationEvent.event_type == RecommendationEventType.WORN.value)
    )
    assert count == 1


async def test_feedback_updates_existing_row(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        first = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/feedback",
            headers={"X-User-Id": str(user_id)},
            json={"feedback_type": "like"},
        )
        second = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/feedback",
            headers={"X-User-Id": str(user_id)},
            json={"feedback_type": "dislike"},
        )

    rows = list(await db_session.scalars(select(OutfitFeedback)))
    assert first.status_code == 200
    assert second.status_code == 200
    assert len(rows) == 1
    assert rows[0].feedback_type == "dislike"


async def test_feedback_bundle_happy_path(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        detail_resp = await client.get(
            f"/v1/recommendations/outfits/{outfit_id}",
            headers={"X-User-Id": str(user_id)},
        )
        items = detail_resp.json()["data"]["items"]
        item_ids = [item["item_id"] for item in items[:2]]
        bundle_payload = {
            "overall": {"feedback_type": "like", "reason_tags": []},
            "items": [
                {"item_id": item_ids[0], "feedback_type": "fits_well", "scene": "commute"},
                {"item_id": item_ids[1], "feedback_type": "tight", "scene": "casual"},
            ],
        }
        response = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/feedback/bundle",
            headers={"X-User-Id": str(user_id)},
            json=bundle_payload,
        )
    assert response.status_code == 200
    assert response.json()["data"]["written"] == 3
    outfit_feedback_count = await db_session.scalar(select(func.count()).select_from(OutfitFeedback))
    fit_feedback_count = await db_session.scalar(select(func.count()).select_from(FitFeedback))
    assert outfit_feedback_count == 1
    assert fit_feedback_count == 2


async def test_feedback_bundle_rollback_on_invalid_item(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)
    invalid_item_id = str(uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        bundle_payload = {
            "overall": {"feedback_type": "like", "reason_tags": []},
            "items": [{"item_id": invalid_item_id, "feedback_type": "fits_well", "scene": "commute"}],
        }
        response = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/feedback/bundle",
            headers={"X-User-Id": str(user_id)},
            json=bundle_payload,
        )
    # 422 — bundle handler validates that each item_id belongs to the
    # outfit before writing anything; an unrelated id is request-body-
    # invalid (4001) rather than a missing-resource (404). The crucial
    # behaviour assertion is below: NO partial writes survive.
    assert response.status_code == 422
    outfit_feedback_count = await db_session.scalar(select(func.count()).select_from(OutfitFeedback))
    fit_feedback_count = await db_session.scalar(select(func.count()).select_from(FitFeedback))
    assert outfit_feedback_count == 0
    assert fit_feedback_count == 0


async def test_feedback_bundle_empty_items(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        response = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/feedback/bundle",
            headers={"X-User-Id": str(user_id)},
            json={"overall": {"feedback_type": "love", "reason_tags": []}, "items": []},
        )
    assert response.status_code == 200
    assert response.json()["data"]["written"] == 1
    outfit_feedback_count = await db_session.scalar(select(func.count()).select_from(OutfitFeedback))
    fit_feedback_count = await db_session.scalar(select(func.count()).select_from(FitFeedback))
    assert outfit_feedback_count == 1
    assert fit_feedback_count == 0


async def test_feedback_bundle_invalid_feedback_type_returns_422(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        response = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/feedback/bundle",
            headers={"X-User-Id": str(user_id)},
            json={"overall": {"feedback_type": "hate", "reason_tags": []}, "items": []},
        )
    assert response.status_code == 422
    outfit_feedback_count = await db_session.scalar(select(func.count()).select_from(OutfitFeedback))
    fit_feedback_count = await db_session.scalar(select(func.count()).select_from(FitFeedback))
    assert outfit_feedback_count == 0
    assert fit_feedback_count == 0
