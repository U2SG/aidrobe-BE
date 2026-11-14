import httpx
import pytest

from app.config.settings import Settings
from app.infrastructure.importers import external_http, link_parser, router


@pytest.fixture(autouse=True)
def public_dns(monkeypatch) -> None:
    async def resolve(host: str, port: int) -> str:
        if host == "169.254.169.254":
            raise external_http.InvalidExternalSource("External URL must resolve to public addresses")
        return "203.0.114.2"

    monkeypatch.setattr(external_http, "_resolve_public_address", resolve)


def _settings() -> Settings:
    return Settings(LINK_PARSE_TIMEOUT_SECONDS=1.0)


async def test_taobao_domain_routes_to_taobao_adapter(monkeypatch) -> None:
    calls: list[str] = []
    async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, headers={"content-type": "text/html"}, text="<html></html>")

    class StubTaobaoAdapter:
        def image_from_html(self, html: str, url: str) -> str | None:
            calls.append(url)
            return "https://img.example.com/item.jpg"

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: async_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    monkeypatch.setitem(router.DOMAIN_ADAPTER_MAP, "taobao.com", StubTaobaoAdapter())

    image_url = await router.route_extract_image_url("https://item.taobao.com/item.htm?id=1", _settings())

    assert image_url == "https://img.example.com/item.jpg"
    assert calls == ["https://item.taobao.com/item.htm?id=1"]


async def test_unknown_domain_falls_back_to_html_parser(monkeypatch) -> None:
    async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            text='<meta property="og:image" content="https://cdn.example.com/web.jpg">',
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: async_client(transport=httpx.MockTransport(handler), **kwargs)
    )

    image_url = await router.route_extract_image_url("https://example.com/product", _settings())

    assert image_url == "https://cdn.example.com/web.jpg"


async def test_ssrf_blocked_url_returns_none(monkeypatch) -> None:
    monkeypatch.undo()
    image_url = await router.route_extract_image_url("http://127.0.0.1/product", _settings())

    assert image_url is None


# --- V3-004 acceptance 4: the image-only entry points behave as before -------
#
# V3-004 made `extract_image_url` / `route_extract_image_url` delegate to the
# ProductPage variants, so every `return None` guard became `return ProductPage()`.
# The import worker (`import_worker.py`), sense-check and style-learn all still
# call the image-only functions, so their guard branches are asserted directly.


async def test_redirect_to_unsafe_url_still_yields_no_image(monkeypatch) -> None:
    async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "169.254.169.254":
            return httpx.Response(
                200,
                request=request,
                text='<meta property="og:image" content="https://cdn.example.com/leak.jpg">',
            )
        return httpx.Response(302, request=request, headers={"location": "http://169.254.169.254/meta"})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: async_client(transport=httpx.MockTransport(handler), **kwargs)
    )

    # Both entry points share the redirect guard.
    assert await router.route_extract_image_url("https://example.com/product", _settings()) is None
    assert await link_parser.HtmlLinkParser(_settings()).extract_image_url("https://example.com/product") is None


async def test_transport_error_still_yields_no_image(monkeypatch) -> None:
    async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: async_client(transport=httpx.MockTransport(handler), **kwargs)
    )

    assert await router.route_extract_image_url("https://example.com/product", _settings()) is None


async def test_image_only_parse_still_fetches_the_page_once(monkeypatch) -> None:
    """Text extraction must not cost a second request: the same fetch that finds
    the image also yields the text, so an import pays exactly what it paid before."""
    async_client = httpx.AsyncClient
    fetches: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        fetches.append(f"{request.url.scheme}://{request.headers['host']}{request.url.path}")
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            text=(
                "<title>Merino Coat</title>"
                '<meta property="og:image" content="https://cdn.example.com/web.jpg">'
            ),
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: async_client(transport=httpx.MockTransport(handler), **kwargs)
    )

    parser = link_parser.HtmlLinkParser(_settings())
    assert await parser.extract_image_url("https://example.com/product") == "https://cdn.example.com/web.jpg"
    assert fetches == ["https://example.com/product"]

    page = await parser.extract_product_page("https://example.com/product")
    assert page.image_url == "https://cdn.example.com/web.jpg"
    assert "Merino Coat" in page.text
    assert len(fetches) == 2  # one fetch per call, not two per call


@pytest.mark.parametrize("status_code", [429, 503])
async def test_import_parser_preserves_retryable_provider_status(monkeypatch, status_code: int) -> None:
    async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: async_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await router.RoutedLinkParser(_settings(), strict=True).extract_image_url("https://example.com/product")
    assert exc_info.value.response.status_code == status_code
