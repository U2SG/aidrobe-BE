from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config.settings import get_settings
from app.infrastructure.logging.setup import setup_logging
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.jobs.scheduler import create_scheduler
from app.presentation.api.v1.routers import (
    health,
    import_tasks,
    recommendations,
    settings,
    uploads,
    wardrobe,
)
from app.presentation.middlewares.exception_handler import setup_exception_handlers


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    scheduler = None
    if settings.ENABLE_SCHEDULER:
        scheduler = create_scheduler(AsyncSessionLocal)
        scheduler.start()
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(title="Aidrobe API", lifespan=lifespan)
    setup_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(settings.router)
    app.include_router(uploads.router)
    app.include_router(import_tasks.router)
    app.include_router(wardrobe.router)
    app.include_router(recommendations.router)
    return app


app = create_app()
