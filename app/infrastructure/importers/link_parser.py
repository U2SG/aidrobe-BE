import ipaddress
import re
import socket
from typing import cast
from urllib.parse import urljoin, urlparse

import httpx

from app.config.settings import Settings

META_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

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


class HtmlLinkParser:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def extract_image_url(self, source_url: str) -> str | None:
        if not is_safe_url(source_url):
            return None
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
                    return None
                response = await client.get(redirect_url)
                hops += 1
            response.raise_for_status()
        html = response.text
        for pattern in (META_IMAGE_RE, IMG_RE):
            match = pattern.search(html)
            if match:
                return cast(str, urljoin(str(response.url), match.group(1)))
        return None
