from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.session import get_db_session

router = APIRouter()


@router.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> JSONResponse:
    """Deep health check: returns 200 when the DB is reachable, 503 otherwise.
    Used by the docker-compose healthcheck and load-balancer probes."""
    try:
        await session.execute(text("SELECT 1"))
        return JSONResponse({"status": "ready"})
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
