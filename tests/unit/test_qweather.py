import httpx

from app.config.settings import Settings
from app.infrastructure.weather.qweather import QWeatherProvider


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value


def _settings() -> Settings:
    return Settings(
        WEATHER_PROVIDER="qweather",
        QWEATHER_API_KEY="test-key",
        QWEATHER_BASE_URL="https://host.re.qweather.com",
        WEATHER_TIMEOUT_SECONDS=1.0,
    )


async def test_free_text_address_resolved_via_geoapi(monkeypatch) -> None:
    paths: list[str] = []
    async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/geo/v2/city/lookup":
            assert request.url.params["location"] == "杭州"
            return httpx.Response(200, request=request, json={"code": "200", "location": [{"id": "101210101"}]})
        assert request.url.params["location"] == "101210101"
        return httpx.Response(
            200,
            request=request,
            json={"now": {"temp": "21", "feelsLike": "20", "text": "多云"}},
        )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: async_client(transport=httpx.MockTransport(handler)))

    provider = QWeatherProvider(_settings(), _FakeRedis())
    weather = await provider.get_weather("杭州")

    assert weather.temperature_c == 21.0
    assert weather.condition == "多云"
    assert paths == ["/geo/v2/city/lookup", "/v7/weather/now"]


async def test_coordinates_skip_geoapi(monkeypatch) -> None:
    paths: list[str] = []
    async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.params["location"] == "116.41,39.92"
        return httpx.Response(
            200,
            request=request,
            json={"now": {"temp": "5", "feelsLike": "2", "text": "晴"}},
        )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: async_client(transport=httpx.MockTransport(handler)))

    provider = QWeatherProvider(_settings(), _FakeRedis())
    weather = await provider.get_weather("116.41,39.92")

    assert weather.temperature_c == 5.0
    assert paths == ["/v7/weather/now"]


async def test_weather_result_is_cached(monkeypatch) -> None:
    calls = {"count": 0}
    async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if request.url.path == "/geo/v2/city/lookup":
            return httpx.Response(200, request=request, json={"code": "200", "location": [{"id": "101210101"}]})
        return httpx.Response(
            200,
            request=request,
            json={"now": {"temp": "21", "feelsLike": "20", "text": "多云"}},
        )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: async_client(transport=httpx.MockTransport(handler)))

    provider = QWeatherProvider(_settings(), _FakeRedis())
    await provider.get_weather("杭州")
    first = calls["count"]
    await provider.get_weather("杭州")

    assert calls["count"] == first  # second call served from cache, no HTTP
