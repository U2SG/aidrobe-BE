import re
from urllib.parse import urljoin

import httpx

from app.infrastructure.importers.link_parser import _is_safe_url

META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
ATTR_RE = re.compile(r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*["']([^"']*)["']""")


def _attrs(tag: str) -> dict[str, str]:
    return {name.lower(): value for name, value in ATTR_RE.findall(tag)}


def _extract_og_image(html: str, base_url: str) -> str | None:
    for tag in META_TAG_RE.findall(html):
        attrs = _attrs(tag)
        if attrs.get("property", attrs.get("name", "")).lower() == "og:image":
            image_url = attrs.get("content")
            if image_url:
                resolved = urljoin(base_url, image_url)
                if _is_safe_url(resolved):
                    return resolved
    return None


class XhsAdapter:
    async def extract_image_url(self, url: str, client: httpx.AsyncClient) -> str | None:
        if not _is_safe_url(url):
            return None
        response = await client.get(url)
        response.raise_for_status()
        return _extract_og_image(response.text, str(response.url))
