import json
import re
from typing import Any
from urllib.parse import urljoin

import httpx

from app.infrastructure.importers.link_parser import _is_safe_url

META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
ATTR_RE = re.compile(r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*["']([^"']*)["']""")
JSON_LD_RE = re.compile(
    r"""<script\b[^>]*type=["']application/ld\+json["'][^>]*>(.*?)</script>""",
    re.IGNORECASE | re.DOTALL,
)


def _attrs(tag: str) -> dict[str, str]:
    return {name.lower(): value for name, value in ATTR_RE.findall(tag)}


def _safe_join(base_url: str, image_url: str) -> str | None:
    resolved = urljoin(base_url, image_url)
    if not _is_safe_url(resolved):
        return None
    return resolved


def _extract_og_image(html: str, base_url: str) -> str | None:
    for tag in META_TAG_RE.findall(html):
        attrs = _attrs(tag)
        if attrs.get("property", attrs.get("name", "")).lower() == "og:image":
            content = attrs.get("content")
            if content:
                return _safe_join(base_url, content)
    return None


def _iter_json_image_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        images: list[str] = []
        for item in value:
            images.extend(_iter_json_image_values(item))
        return images
    if isinstance(value, dict):
        for key in ("url", "contentUrl"):
            nested = value.get(key)
            if isinstance(nested, str):
                return [nested]
    return []


def _find_json_ld_images(value: Any) -> list[str]:
    if isinstance(value, dict):
        images: list[str] = []
        for key, nested in value.items():
            if key in {"image", "thumbnailUrl"}:
                images.extend(_iter_json_image_values(nested))
            else:
                images.extend(_find_json_ld_images(nested))
        return images
    if isinstance(value, list):
        images = []
        for item in value:
            images.extend(_find_json_ld_images(item))
        return images
    return []


def _extract_json_ld_image(html: str, base_url: str) -> str | None:
    for match in JSON_LD_RE.finditer(html):
        try:
            payload = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue
        for image_url in _find_json_ld_images(payload):
            resolved = _safe_join(base_url, image_url)
            if resolved is not None:
                return resolved
    return None


def _extract_booth_image(html: str, base_url: str) -> str | None:
    for tag in IMG_TAG_RE.findall(html):
        attrs = _attrs(tag)
        if attrs.get("id") == "J_ImgBooth":
            image_url = attrs.get("src")
            if image_url:
                return _safe_join(base_url, image_url)
    return None


class TaobaoAdapter:
    async def extract_image_url(self, url: str, client: httpx.AsyncClient) -> str | None:
        if not _is_safe_url(url):
            return None
        response = await client.get(url)
        response.raise_for_status()
        html = response.text
        base_url = str(response.url)
        return (
            _extract_og_image(html, base_url)
            or _extract_json_ld_image(html, base_url)
            or _extract_booth_image(html, base_url)
        )
