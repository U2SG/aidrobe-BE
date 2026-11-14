import re
from dataclasses import dataclass
from typing import cast
from urllib.parse import urljoin

import httpx

from app.config.settings import Settings
from app.infrastructure.importers.external_http import BROWSER_HEADERS as BROWSER_HEADERS
from app.infrastructure.importers.external_http import InvalidExternalSource, fetch_external
from app.infrastructure.importers.external_http import is_safe_url as is_safe_url
from app.infrastructure.storage.object_key import names_internal_storage

META_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

# V3-004 — product text, for the attribute hints a product image alone cannot
# carry (material, price, occasion). Title + og/twitter description + og:title
# are the fields shopping sites reliably fill.
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
META_TEXT_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']'
    r'(?:og:title|og:description|twitter:title|twitter:description|description|keywords)'
    r'["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProductPage:
    """What one fetch of a product page yields.

    `text` is RAW UNTRUSTED page content — it must be bounded/sanitized before
    it is read for attributes or ever reaches a prompt (V3-004 acceptance 3).
    Callers get it unsanitized on purpose: sanitizing here would hide the
    boundary that the ingest path is responsible for enforcing.
    """

    image_url: str | None = None
    text: str = ""

_is_safe_url = is_safe_url
_MAX_PRODUCT_PAGE_BYTES = 2 * 1024 * 1024


async def fetch_product_html(source_url: str, settings: Settings) -> tuple[str, str]:
    response, final_url = await fetch_external(
        source_url,
        settings,
        timeout=settings.LINK_PARSE_TIMEOUT_SECONDS,
        max_bytes=_MAX_PRODUCT_PAGE_BYTES,
        accepts=lambda content_type: content_type in {"text/html", "application/xhtml+xml"},
    )
    return response.text, final_url


def safe_product_image_url(image_url: str | None, settings: Settings) -> str | None:
    if image_url and not names_internal_storage(image_url, settings):
        return image_url
    return None


def image_from_html(html: str, base_url: str) -> str | None:
    for pattern in (META_IMAGE_RE, IMG_RE):
        match = pattern.search(html)
        if match:
            return cast(str, urljoin(base_url, match.group(1)))
    return None


def text_from_html(html: str) -> str:
    """Title + description/keywords meta values, joined. Still raw and untrusted."""
    parts: list[str] = []
    title = TITLE_RE.search(html)
    if title:
        parts.append(title.group(1))
    parts.extend(META_TEXT_RE.findall(html))
    return " ".join(part for part in parts if part)


class HtmlLinkParser:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def extract_image_url(self, source_url: str) -> str | None:
        return (await self.extract_product_page(source_url)).image_url

    async def extract_product_page(self, source_url: str) -> ProductPage:
        """Fetch once, return both the product image and the page text.

        `extract_image_url` delegates here so there is a single fetch
        implementation and a single redirect-revalidation path — a second fetch
        for the text would double the SSRF surface for no gain.
        """
        try:
            html, final_url = await fetch_product_html(source_url, self.settings)
        except (InvalidExternalSource, httpx.HTTPError, TimeoutError):
            return ProductPage()
        return ProductPage(
            image_url=safe_product_image_url(image_from_html(html, final_url), self.settings),
            text=text_from_html(html),
        )
