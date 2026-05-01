import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
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


async def test_feedback_accepts_only_like_dislike(db_session: AsyncSession) -> None:
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
            json={"feedback_type": "worn"},
        )

    count = await db_session.scalar(select(func.count()).select_from(OutfitFeedback))
    assert response.status_code == 200
    assert invalid.status_code == 422
    assert count == 1
