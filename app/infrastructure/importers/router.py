from typing import Protocol
from urllib.parse import urljoin, urlparse

import httpx

from app.config.settings import Settings
from app.infrastructure.importers.adapters.douyin import DouyinAdapter
from app.infrastructure.importers.adapters.taobao import TaobaoAdapter
from app.infrastructure.importers.adapters.xhs import XhsAdapter
from app.infrastructure.importers.link_parser import (
    BROWSER_HEADERS,
    HtmlLinkParser,
    ProductPage,
    _is_safe_url,
)


class ImportAdapter(Protocol):
    async def extract_image_url(self, url: str, client: httpx.AsyncClient) -> str | None: ...


DOMAIN_ADAPTER_MAP: dict[str, ImportAdapter] = {
    "taobao.com": TaobaoAdapter(),
    "tmall.com": TaobaoAdapter(),
    "xiaohongshu.com": XhsAdapter(),
    "xhslink.com": XhsAdapter(),
    "douyin.com": DouyinAdapter(),
    "v.douyin.com": DouyinAdapter(),
}


def _adapter_for_url(url: str) -> ImportAdapter | None:
    hostname = urlparse(url).hostname
    if hostname is None:
        return None
    hostname = hostname.lower()
    for domain, adapter in DOMAIN_ADAPTER_MAP.items():
        if hostname == domain or hostname.endswith(f".{domain}"):
            return adapter
    return None


async def _expand_short_links(source_url: str, client: httpx.AsyncClient) -> str | None:
    if not _is_safe_url(source_url):
        return None

    current_url = source_url
    hops = 0
    while hops < 5:
        response = await client.get(current_url)
        if not response.is_redirect:
            return str(response.url)

        location = response.headers.get("location", "")
        redirect_url = urljoin(str(response.url), location)
        if not _is_safe_url(redirect_url):
            return None
        current_url = redirect_url
        hops += 1

    return current_url


async def route_extract_image_url(source_url: str, settings: Settings) -> str | None:
    return (await route_extract_product_page(source_url, settings)).image_url


async def route_extract_product_page(source_url: str, settings: Settings) -> ProductPage:
    """V3-004 — same routing as `route_extract_image_url`, but also returns the
    page text on the generic-HTML branch.

    Per-domain adapters (Taobao / XHS / Douyin) resolve an image out of
    JS-driven pages and expose no page text, so their branch returns
    image-only. Nothing here sanitizes `ProductPage.text` — that is the ingest
    path's boundary.
    """
    try:
        async with httpx.AsyncClient(
            timeout=settings.LINK_PARSE_TIMEOUT_SECONDS,
            follow_redirects=False,
            headers=BROWSER_HEADERS,
        ) as client:
            expanded_url = await _expand_short_links(source_url, client)
            if expanded_url is None:
                return ProductPage()

            adapter = _adapter_for_url(expanded_url)
            if adapter is not None:
                return ProductPage(image_url=await adapter.extract_image_url(expanded_url, client))

        return await HtmlLinkParser(settings).extract_product_page(expanded_url)
    except (httpx.RequestError, httpx.HTTPStatusError):
        return ProductPage()


class RoutedLinkParser:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def extract_image_url(self, source_url: str) -> str | None:
        return await route_extract_image_url(source_url, self.settings)

    async def extract_product_page(self, source_url: str) -> ProductPage:
        return await route_extract_product_page(source_url, self.settings)
