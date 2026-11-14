from typing import Protocol
from urllib.parse import urlparse

import httpx

from app.config.settings import Settings
from app.infrastructure.importers.adapters.douyin import DouyinAdapter
from app.infrastructure.importers.adapters.taobao import TaobaoAdapter
from app.infrastructure.importers.adapters.xhs import XhsAdapter
from app.infrastructure.importers.external_http import InvalidExternalSource
from app.infrastructure.importers.link_parser import (
    ProductPage,
    fetch_product_html,
    image_from_html,
    safe_product_image_url,
    text_from_html,
)


class ImportAdapter(Protocol):
    def image_from_html(self, html: str, url: str) -> str | None: ...


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


async def route_extract_image_url(source_url: str, settings: Settings) -> str | None:
    return (await route_extract_product_page(source_url, settings)).image_url


async def route_extract_product_page(
    source_url: str, settings: Settings, *, strict: bool = False
) -> ProductPage:
    try:
        html, final_url = await fetch_product_html(source_url, settings)
        adapter = _adapter_for_url(final_url)
        if adapter is not None:
            # Domain-specific markup has its own image parsing rules.
            return ProductPage(image_url=safe_product_image_url(adapter.image_from_html(html, final_url), settings))
        return ProductPage(
            image_url=safe_product_image_url(image_from_html(html, final_url), settings),
            text=text_from_html(html),
        )
    except (InvalidExternalSource, httpx.HTTPError, TimeoutError):
        if strict:
            raise
        return ProductPage()


class RoutedLinkParser:
    def __init__(self, settings: Settings, *, strict: bool = False) -> None:
        self.settings = settings
        self.strict = strict

    async def extract_image_url(self, source_url: str) -> str | None:
        return (await self.extract_product_page(source_url)).image_url

    async def extract_product_page(self, source_url: str) -> ProductPage:
        return await route_extract_product_page(source_url, self.settings, strict=self.strict)
