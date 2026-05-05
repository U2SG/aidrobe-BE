import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import Category, ItemStatus
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.fit import FitFeedback
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import fit as fit_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def build_fit_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return Settings(APP_ENV="dev")

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(fit_router.router)
    return app


async def seed_item(session: AsyncSession, user_id: uuid.UUID) -> WardrobeItem:
    session.add(User(id=user_id))
    item = WardrobeItem(
        user_id=user_id,
        title="White shirt",
        category=Category.TOP.value,
        color_main="white",
        status=ItemStatus.WEARABLE.value,
    )
    session.add(item)
    await session.flush()
    return item


async def test_patch_fit_updates_only_provided_fields(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    item = await seed_item(db_session, user_id)
    app = build_fit_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.patch(
            f"/v1/wardrobe-items/{item.id}/fit",
            headers={"X-User-Id": str(user_id)},
            json={
                "size_label": "M",
                "size_region": "CN",
                "fit_intent": "regular",
                "fit_status": "fits_well",
                "comfort_score": 5,
            },
        )

    await db_session.refresh(item)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["size_label"] == "M"
    assert data["size_region"] == "CN"
    assert data["fit_status"] == "fits_well"
    assert data["comfort_score"] == 5
    assert item.title == "White shirt"


async def test_patch_fit_returns_404_for_other_user(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    item = await seed_item(db_session, user_id)
    app = build_fit_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.patch(
            f"/v1/wardrobe-items/{item.id}/fit",
            headers={"X-User-Id": str(other_user_id)},
            json={"fit_status": "tight"},
        )

    assert response.status_code == 404
    assert response.json()["code"] == 4041


async def test_post_fit_feedback_updates_item_fit_status(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    item = await seed_item(db_session, user_id)
    app = build_fit_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/fit-feedback",
            headers={"X-User-Id": str(user_id)},
            json={
                "item_id": str(item.id),
                "feedback_type": "tight",
                "scene": "commute",
            },
        )

    await db_session.refresh(item)
    feedback = await db_session.scalar(select(FitFeedback).where(FitFeedback.item_id == item.id))
    assert response.status_code == 200
    assert response.json()["data"]["fit_status"] == "tight"
    assert item.fit_status == "tight"
    assert feedback is not None
    assert feedback.feedback_type == "tight"


async def test_fit_rejects_invalid_enum(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    item = await seed_item(db_session, user_id)
    app = build_fit_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.patch(
            f"/v1/wardrobe-items/{item.id}/fit",
            headers={"X-User-Id": str(user_id)},
            json={"fit_status": "baggy"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == 4001
