"""Shared read-path URL signing helper.

Internal-storage rows persist a stable `object_key`; reads materialize a
short-lived signed URL at request time (never persisting the TTL-bound URL).
External (import-flow) URLs have no key and pass through as-is.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.storage import ObjectStoragePort
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.wardrobe_repo import ImageSource, load_wardrobe_image_sources


def resolve_render_url(
    object_key: str | None, fallback_url: str | None, *, storage: ObjectStoragePort | None = None
) -> str | None:
    """Sign and return a short-lived URL when an `object_key` is present;
    otherwise return the persisted URL as-is. If signing fails (storage
    misconfigured) fall through to the persisted URL rather than 500-ing
    the read."""
    if object_key:
        from app.bootstrap.container import object_storage

        try:
            return (storage or object_storage()).sign_render_url(object_key)
        except Exception:
            pass
    return fallback_url


@dataclass(frozen=True)
class WardrobeImageUrls:
    cover: str | None = None
    original: str | None = None


async def wardrobe_image_urls(
    session: AsyncSession, items: Sequence[WardrobeItem]
) -> dict[uuid.UUID, WardrobeImageUrls]:
    """Resolve image references for already-authorized wardrobe rows in one query."""
    from app.bootstrap.container import object_storage

    storage: ObjectStoragePort | None = None

    def render(source: ImageSource) -> str | None:
        nonlocal storage
        key, url = source
        if not key:
            return url
        try:
            if storage is None:
                storage = object_storage()
        except Exception:
            return None
        return resolve_render_url(key, None, storage=storage)

    result: dict[uuid.UUID, WardrobeImageUrls] = {}
    for item_id, (cover, original) in (await load_wardrobe_image_sources(session, items)).items():
        original_url = render(original)
        cover_url = render(cover) if cover != original else original_url
        result[item_id] = WardrobeImageUrls(cover=cover_url or original_url, original=original_url)
    return result
