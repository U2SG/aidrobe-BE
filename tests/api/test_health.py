from httpx import AsyncClient


async def test_health_check(async_client: AsyncClient) -> None:
    response = await async_client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_version_reports_git_sha_and_schema_revision(async_client: AsyncClient) -> None:
    response = await async_client.get("/version")

    assert response.status_code == 200
    body = response.json()
    assert body["git_sha"] == "unknown"
    assert "alembic_revision" in body
