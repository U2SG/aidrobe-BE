import re
from urllib.parse import urljoin

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
                return urljoin(base_url, image_url)
    return None


class XhsAdapter:
    def image_from_html(self, html: str, url: str) -> str | None:
        return _extract_og_image(html, url)
