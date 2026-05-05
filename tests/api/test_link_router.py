import httpx

from app.config.settings import Settings
from app.infrastructure.importers import link_parser, router


def _settings() -> Settings:
    return Settings(LINK_PARSE_TIMEOUT_SECONDS=1.0)


async def test_taobao_domain_routes_to_taobao_adapter(monkeypatch) -> None:
    calls: list[str] = []
    async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, text="<html></html>")

    class StubTaobaoAdapter:
        async def extract_image_url(self, url: str, client: httpx.AsyncClient) -> str | None:
            calls.append(url)
            return "https://img.example.com/item.jpg"

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: async_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setitem(router.DOMAIN_ADAPTER_MAP, "taobao.com", StubTaobaoAdapter())
    monkeypatch.setattr(router, "_is_safe_url", lambda url: True)

    image_url = await router.route_extract_image_url("https://item.taobao.com/item.htm?id=1", _settings())

    assert image_url == "https://img.example.com/item.jpg"
    assert calls == ["https://item.taobao.com/item.htm?id=1"]


async def test_unknown_domain_falls_back_to_html_parser(monkeypatch) -> None:
    async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            text='<meta property="og:image" content="https://cdn.example.com/web.jpg">',
        )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: async_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(router, "_is_safe_url", lambda url: True)
    monkeypatch.setattr(link_parser, "is_safe_url", lambda url: True)

    image_url = await router.route_extract_image_url("https://example.com/product", _settings())

    assert image_url == "https://cdn.example.com/web.jpg"


async def test_ssrf_blocked_url_returns_none() -> None:
    image_url = await router.route_extract_image_url("http://127.0.0.1/product", _settings())

    assert image_url is None
