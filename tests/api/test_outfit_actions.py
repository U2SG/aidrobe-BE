import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.fit import FitFeedback
from app.infrastructure.persistence.models.recommendation import OutfitFeedback, WearHistory
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
