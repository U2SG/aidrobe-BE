"""Register the same delivery port in API and unified worker lifecycles."""

from app.common.analytics_delivery import set_analytics_sink
from app.config.settings import get_settings
from app.infrastructure.analytics_transport import AnalyticsPump, HttpAnalyticsSink

_pump: AnalyticsPump | None = None


async def start_analytics() -> None:
    global _pump
    await stop_analytics()
    settings = get_settings()
    _pump = AnalyticsPump(
        HttpAnalyticsSink(settings).send,
        enabled=lambda: settings.ENABLE_V3_ANALYTICS,
    )
    set_analytics_sink(_pump.enqueue)


async def stop_analytics() -> None:
    global _pump
    set_analytics_sink(None)
    pump, _pump = _pump, None
    if pump is not None:
        await pump.close()


async def analytics_worker_startup(ctx: object) -> None:
    await start_analytics()


async def analytics_worker_shutdown(ctx: object) -> None:
    await stop_analytics()
