import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.user_repo import SqlAlchemyUserRepository
from app.infrastructure.persistence.session import get_db_session
from app.infrastructure.security.jwt.tokens import JwtTokenService
from app.presentation.api.v1.deps import get_current_user
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def build_test_app(session: AsyncSession, settings: Settings) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return settings

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings

    @app.get("/me")
    async def me(user: Annotated[User, Depends(get_current_user)]) -> dict[str, str]:
        return {"user_id": str(user.id)}

    return app


async def test_missing_x_user_id_returns_401(db_session: AsyncSession) -> None:
    app = build_test_app(db_session, Settings(APP_ENV="dev"))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/me")

    assert response.status_code == 401
    assert response.json()["code"] == 4002


async def test_valid_x_user_id_resolves_or_creates_user(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_test_app(db_session, Settings(APP_ENV="dev"))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/me", headers={"X-User-Id": str(user_id)})

    assert response.status_code == 200
    assert response.json() == {"user_id": str(user_id)}
    assert await db_session.get(User, user_id) is not None


async def test_x_user_id_disabled_in_prod(db_session: AsyncSession) -> None:
    app = build_test_app(db_session, Settings(APP_ENV="prod"))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/me", headers={"X-User-Id": str(uuid.uuid4())})

    assert response.status_code == 401
    assert response.json()["code"] == 4002


async def test_bearer_token_resolves_user_in_prod(db_session: AsyncSession) -> None:
    settings = Settings(APP_ENV="prod", JWT_SECRET="test-secret-with-at-least-32-bytes")
    repository = SqlAlchemyUserRepository(db_session)
    user = await repository.get_or_create(uuid.uuid4())
    token_service = JwtTokenService(settings)
    session = await repository.create_session(
        user.id,
        token_service.hash_token("refresh-token"),
        datetime.now(UTC) + timedelta(days=30),
    )
    access_token = token_service.create_access_token(user.id, session.id)
    app = build_test_app(db_session, settings)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/me", headers={"Authorization": f"Bearer {access_token}"})

    assert response.status_code == 200
    assert response.json() == {"user_id": str(user.id)}
