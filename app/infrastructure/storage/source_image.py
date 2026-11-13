"""Read an image the user imported, wherever it lives.

Shared by the pipelines that post-process uploads (cutout, garment crops):
our own storage is read by key (no HTTP, no SSRF surface); anything else is an
external import link fetched over HTTP behind the SSRF guard.
"""

from __future__ import annotations

import httpx

from app.infrastructure.importers.link_parser import BROWSER_HEADERS, is_safe_url


def key_from_url(url: str, bucket: str) -> str | None:
    """Derive the storage object key from an internal file URL.

    Internal URLs are `<base>/<bucket>/<key>` (S3/MinIO) regardless of which
    host the base used, so splitting on `/<bucket>/` is host-agnostic. Returns
    None for URLs that don't reference our bucket (external import links)."""
    marker = f"/{bucket}/"
    idx = url.find(marker)
    if idx == -1:
        return None
    return url[idx + len(marker) :]


async def load_source_bytes(source_url: str, source_key: str | None) -> bytes:
    """Read the original image. Prefer reading from our own storage by key
    (no HTTP, no SSRF guard); fall back to an HTTP fetch for external URLs."""
    from app.bootstrap.container import object_storage
    from app.config.settings import get_settings

    storage = object_storage()
    key = source_key
    if key is None:
        bucket = get_settings().S3_BUCKET
        if bucket:
            key = key_from_url(source_url, bucket)
    if key is not None:
        return await storage.get_object(key)

    # External URL (user-pasted import link) — guard against SSRF.
    if not is_safe_url(source_url):
        raise ValueError(f"unsafe source url blocked: {source_url}")
    async with httpx.AsyncClient(timeout=30, headers=BROWSER_HEADERS) as client:
        resp = await client.get(source_url, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
