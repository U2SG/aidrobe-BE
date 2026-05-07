import uuid

import pytest
from httpx import AsyncClient


@pytest.fixture(scope="session", autouse=True)
def create_schema() -> None:
    pass


async def test_process_time_header_is_valid_integer(async_client: AsyncClient) -> None:
    response = await async_client.get("/healthz")

    process_time = response.headers["X-Process-Time-Ms"]
    assert process_time.isdecimal()
    assert int(process_time) >= 0


async def test_correlation_id_is_echoed(async_client: AsyncClient) -> None:
    correlation_id = "test-correlation-id"

    response = await async_client.get(
        "/healthz",
        headers={"X-Correlation-Id": correlation_id},
    )

    assert response.headers["X-Correlation-Id"] == correlation_id


async def test_correlation_id_is_generated(async_client: AsyncClient) -> None:
    response = await async_client.get("/healthz")

    correlation_id = response.headers["X-Correlation-Id"]
    parsed = uuid.UUID(correlation_id)
    assert str(parsed) == correlation_id
    assert parsed.version == 4
