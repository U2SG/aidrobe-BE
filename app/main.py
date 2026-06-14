from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sentry_sdk.integrations.fastapi import FastApiIntegration

from app.config.settings import get_settings
from app.infrastructure.cache.client import close_redis_client
from app.infrastructure.logging.setup import setup_logging
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.jobs.scheduler import create_scheduler
from app.presentation.api.v1.routers import (
    account,
    admin,
    auth,
    fit,
    health,
    import_tasks,
    insights,
    ootd_posters,
    outfit_history,
    outfit_plan,
    outfit_visual,
    packing_list,
    recommendations,
    settings,
    shopping,
    style,
    uploads,
    wardrobe,
)
from app.presentation.middlewares.correlation import CorrelationIdMiddleware
from app.presentation.middlewares.exception_handler import setup_exception_handlers
from app.presentation.middlewares.rate_limit import InMemoryRateLimitMiddleware
from app.presentation.middlewares.timing import TimingMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    if settings.SENTRY_DSN:
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.SENTRY_ENVIRONMENT or settings.APP_ENV,
            traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
            integrations=[FastApiIntegration()],
        )
    scheduler = None
    if settings.ENABLE_SCHEDULER:
        scheduler = create_scheduler(AsyncSessionLocal)
        scheduler.start()
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown()
        await close_redis_client()


def create_app() -> FastAPI:
    cfg = get_settings()
    app = FastAPI(title="Aidrobe API", lifespan=lifespan)
    # CORS for browser (web) clients. Native apps are unaffected. Dev/test allow
    # any localhost origin; prod only when CORS_ALLOW_ORIGINS is configured.
    explicit_origins = [o.strip() for o in cfg.CORS_ALLOW_ORIGINS.split(",") if o.strip()]
    if cfg.APP_ENV != "prod":
        app.add_middleware(
            CORSMiddleware,
            allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=False,
        )
    elif explicit_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=explicit_origins,
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=False,
        )
    app.add_middleware(InMemoryRateLimitMiddleware, settings=cfg)
    app.add_middleware(TimingMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    setup_exception_handlers(app)
    app.include_router(auth.router)
    app.include_router(health.router)
    app.include_router(settings.router)
    app.include_router(uploads.router)
    app.include_router(import_tasks.router)
    app.include_router(wardrobe.router)
    app.include_router(fit.router)
    app.include_router(recommendations.router)
    app.include_router(style.router)
    app.include_router(insights.router)
    app.include_router(outfit_history.router)
    app.include_router(outfit_plan.router)
    app.include_router(outfit_visual.router)
    app.include_router(ootd_posters.router)
    app.include_router(packing_list.router)
    app.include_router(shopping.router)
    app.include_router(admin.router)
    app.include_router(account.router)
    return app


app = create_app()
