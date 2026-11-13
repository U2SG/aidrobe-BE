from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.infrastructure.cache.client import get_redis_client
from app.infrastructure.persistence.session import get_db_session

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> JSONResponse:
    """Deep health check: 200 only when the DB and Redis both answer.
    Used by the docker-compose healthcheck and load-balancer probes."""
    checks: dict[str, str] = {}
    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"
    try:
        await get_redis_client().ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "unavailable"
    if all(value == "ok" for value in checks.values()):
        return JSONResponse({"status": "ready", "checks": checks})
    logger.warning("readiness_failed", checks=checks)
    return JSONResponse({"status": "unavailable", "checks": checks}, status_code=503)


@router.get("/version")
async def version(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, str | None]:
    """Which code and which schema this instance runs; null revision when the
    database has no alembic_version table (e.g. schema built without Alembic)."""
    try:
        revision = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
    except Exception:
        await session.rollback()
        revision = None
    return {"git_sha": get_settings().GIT_SHA, "alembic_revision": revision}
