import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from app.infrastructure.persistence.models import Base
from app.main import create_app


@pytest.fixture
async def async_client() -> AsyncClient:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    pgvector = os.getenv("PGVECTOR_ENABLED", "false").lower() in ("1", "true", "yes")
    image = "pgvector/pgvector:pg16" if pgvector else "postgres:16-alpine"
    with PostgresContainer(image) as postgres:
        url = postgres.get_connection_url()
        yield url.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
            "postgresql://", "postgresql+asyncpg://"
        )


@pytest.fixture(scope="session")
def sync_db_url(postgres_url: str) -> str:
    return postgres_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@pytest.fixture(scope="session", autouse=True)
def create_schema(sync_db_url: str) -> None:
    # Use a synchronous engine to create the schema once for the whole session.
    # This avoids sharing asyncpg connections across event loops.
    engine = create_engine(sync_db_url)
    Base.metadata.create_all(engine)
    engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _truncate_between_tests(sync_db_url: str) -> AsyncIterator[None]:
    """Phase C+ broke per-test isolation: workers use AsyncSessionLocal to
    open ad-hoc sessions on a separate engine and `await session.commit()`
    inside (e.g. claim_one's commit-on-claim, mark_succeeded). Those commits
    land in the postgres container and survive the per-test fixture
    rollback because they're on a different connection.

    Truncating before each test is the simplest reliable fix — Postgres
    TRUNCATE CASCADE on a small fixture set takes <30 ms. Done in a sync
    engine so we don't have to thread asyncpg event loops here."""
    engine = create_engine(sync_db_url)
    with engine.connect() as conn:
        names = [t.name for t in Base.metadata.sorted_tables]
        if names:
            conn.execute(
                text(f'TRUNCATE TABLE {", ".join(names)} RESTART IDENTITY CASCADE')
            )
            conn.commit()
    engine.dispose()
    yield


@pytest_asyncio.fixture
async def db_session(postgres_url: str) -> AsyncIterator[AsyncSession]:
    # Each test gets its own engine + connection + transaction.
    # The autouse `_truncate_between_tests` fixture below cleans up data
    # committed via separate engines (workers using AsyncSessionLocal),
    # which is the leakage path savepoint-mode would otherwise paper
    # over. We avoid savepoint mode because it changes flush ordering
    # in subtle ways that broke FK-dependency inserts in some tests.
    engine = create_async_engine(postgres_url)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(bind=connection, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            if transaction.is_active:
                await transaction.rollback()
    await engine.dispose()


@pytest.fixture
def import_api_environment(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run legacy background imports with separate sessions on the fixture connection."""
    from app.infrastructure.workers import import_worker

    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(import_worker, "AsyncSessionLocal", factory)

    async def download(url: str, settings: object) -> tuple[bytes, str]:
        return url.encode(), "image/jpeg"

    async def load(url: str, key: str, **kwargs: object) -> bytes:
        return url.encode()

    class Storage:
        async def put_object(self, key: str, data: bytes, content_type: str) -> str:
            return f"http://localhost:9000/local/files/{key}/{data.decode().rsplit('/', 1)[-1]}"

    # Stub transport and encoding here. Storage security and image decoding
    # run against real bytes in the separate import pipeline regressions.
    monkeypatch.setattr(import_worker, "download_external_source_image", download)
    monkeypatch.setattr(import_worker, "load_source_bytes", load)
    monkeypatch.setattr(import_worker, "image_data_uri", bytes.decode)
    monkeypatch.setattr(import_worker, "object_storage", Storage)
