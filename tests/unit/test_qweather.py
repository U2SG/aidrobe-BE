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


_NOW = {
    "now": {
        "temp": "21",
        "feelsLike": "20",
        "text": "多云",
        "humidity": "63",
        "windDir": "东南风",
        "windScale": "2",
        "windSpeed": "11",
        "icon": "101",
    }
}
_HOURLY = {"hourly": [{"pop": "10"}, {"pop": "40"}, {"pop": "25"}]}
_GEO = {"code": "200", "location": [{"id": "101210101", "name": "杭州"}]}


def _provider() -> QWeatherProvider:
    return QWeatherProvider(_settings(), _FakeRedis())


def _route(monkeypatch, handler) -> None:
    async_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: async_client(transport=httpx.MockTransport(handler)))


async def test_free_text_address_resolved_via_geoapi(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/geo/v2/city/lookup":
            assert request.url.params["location"] == "杭州"
            return httpx.Response(200, request=request, json=_GEO)
        if path == "/v7/weather/24h":
            return httpx.Response(200, request=request, json=_HOURLY)
        assert request.url.params["location"] == "101210101"
        return httpx.Response(200, request=request, json=_NOW)

    _route(monkeypatch, handler)
    weather = await _provider().get_weather("杭州")

    assert weather.temperature_c == 21.0
    assert weather.condition == "多云"
    assert weather.location_name == "杭州"
    # new now fields surfaced
    assert weather.humidity == 63
    assert weather.wind_dir == "东南风"
    assert weather.wind_scale == "2"
    assert weather.wind_speed == 11.0
    assert weather.icon == "101"
    # pop: max(10, 40, 25) / 100
    assert weather.rain_probability == 0.4


async def test_coordinates_skip_geo_for_id_but_reverse_lookup_name(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/geo/v2/city/lookup":
            # reverse lookup uses the coords directly
            assert request.url.params["location"] == "116.41,39.92"
            return httpx.Response(200, request=request, json={"location": [{"id": "x", "name": "北京"}]})
        if path == "/v7/weather/24h":
            return httpx.Response(200, request=request, json={"hourly": []})
        assert request.url.params["location"] == "116.41,39.92"
        return httpx.Response(200, request=request, json=_NOW)

    _route(monkeypatch, handler)
    weather = await _provider().get_weather("116.41,39.92")

    assert weather.temperature_c == 21.0
    assert weather.location_name == "北京"
    assert weather.rain_probability == 0.0  # empty hourly degrades to 0


async def test_pop_failure_degrades_to_zero(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/geo/v2/city/lookup":
            return httpx.Response(200, request=request, json={"location": [{"id": "x", "name": "城"}]})
        if path == "/v7/weather/24h":
            return httpx.Response(500, request=request, text="boom")
        return httpx.Response(200, request=request, json=_NOW)

    _route(monkeypatch, handler)
    weather = await _provider().get_weather("116.41,39.92")

    assert weather.temperature_c == 21.0
    assert weather.rain_probability == 0.0


async def test_empty_optional_now_fields_parse_to_none(monkeypatch) -> None:
    blank_now = {"now": {"temp": "5", "feelsLike": "2", "text": "晴", "humidity": "", "windSpeed": ""}}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/geo/v2/city/lookup":
            return httpx.Response(200, request=request, json={"location": []})
        if path == "/v7/weather/24h":
            return httpx.Response(200, request=request, json={"hourly": [{"pop": "5"}]})
        return httpx.Response(200, request=request, json=blank_now)

    _route(monkeypatch, handler)
    weather = await _provider().get_weather("116.41,39.92")

    assert weather.temperature_c == 5.0
    assert weather.humidity is None
    assert weather.wind_speed is None
    assert weather.location_name is None  # empty geo match -> no name


async def test_weather_result_is_cached(monkeypatch) -> None:
    calls = {"count": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        path = request.url.path
        if path == "/geo/v2/city/lookup":
            return httpx.Response(200, request=request, json=_GEO)
        if path == "/v7/weather/24h":
            return httpx.Response(200, request=request, json=_HOURLY)
        return httpx.Response(200, request=request, json=_NOW)

    _route(monkeypatch, handler)
    provider = _provider()
    await provider.get_weather("杭州")
    first = calls["count"]
    await provider.get_weather("杭州")

    assert calls["count"] == first  # second call served from cache, no HTTP
