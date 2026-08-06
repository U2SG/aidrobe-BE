import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import cast
from urllib.parse import urljoin, urlparse

import httpx

from app.config.settings import Settings

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

# A default httpx user-agent gets 403'd or served a bot page by most sites
# (Wikipedia, Taobao, etc.). Present as a normal browser; zh Accept-Language
# helps the CN shopping sites return product markup.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_ALLOWED_SCHEMES = {"http", "https"}


def is_safe_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    # Block localhost and common internal names
    if hostname in {"localhost", "metadata.google.internal"}:
        return False
    try:
        addr = ipaddress.ip_address(socket.gethostbyname(hostname))
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            return False
    except (socket.gaierror, ValueError):
        return False
    return True


_is_safe_url = is_safe_url


def _image_from_html(html: str, base_url: str) -> str | None:
    for pattern in (META_IMAGE_RE, IMG_RE):
        match = pattern.search(html)
        if match:
            return cast(str, urljoin(base_url, match.group(1)))
    return None


def _text_from_html(html: str) -> str:
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
        if not is_safe_url(source_url):
            return ProductPage()
        async with httpx.AsyncClient(
            timeout=self.settings.LINK_PARSE_TIMEOUT_SECONDS,
            follow_redirects=False,
            headers=BROWSER_HEADERS,
        ) as client:
            response = await client.get(source_url)
            # Follow redirects manually so we can re-check each hop
            hops = 0
            while response.is_redirect and hops < 5:
                redirect_url = response.headers.get("location", "")
                if not is_safe_url(redirect_url):
                    return ProductPage()
                response = await client.get(redirect_url)
                hops += 1
            response.raise_for_status()
        html = response.text
        return ProductPage(
            image_url=_image_from_html(html, str(response.url)),
            text=_text_from_html(html),
        )
