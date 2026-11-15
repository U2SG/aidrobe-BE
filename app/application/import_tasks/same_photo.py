"""Match a garment re-imported from the same photograph."""

import hashlib
import uuid
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.purchase.garment_type import garment_type
from app.config.settings import get_settings
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.storage.object_key import extract_internal_object_key, is_owned_object_key


def _identity(category: str | None, *names: str | None) -> tuple[str | None, str]:
    return category, garment_type(*names) or next((name.strip().casefold() for name in names if name), "")


async def existing_photo_item(
    session: AsyncSession,
    user_id: uuid.UUID,
    source: str | None,
    candidate: dict[str, Any],
    siblings: list[dict[str, Any]],
    confirmed_category: str,
    *,
    fingerprint: str | None = None,
    lock_photo: bool = True,
) -> WardrobeItem | None:
    category = candidate.get("category")
    if not source or category != confirmed_category:
        return None
    identity = _identity(category, candidate.get("canonical_name"), candidate.get("title"))
    if not identity[1] or sum(
        _identity(raw.get("category"), raw.get("canonical_name"), raw.get("title")) == identity
        for raw in siblings
    ) > 1:
        return None

    key = extract_internal_object_key(source, get_settings())
    owned_key = key if key and is_owned_object_key(key, user_id) else None
    # Fresh tasks have different IDs. Serialize confirmations of the same photo.
    if lock_photo:
        lock_parts = {owned_key or source}
        if fingerprint:
            lock_parts.add(fingerprint)
        for part in sorted(lock_parts):
            digest = hashlib.sha256(f"same-import-photo:{user_id}:{part}".encode()).digest()
            lock = int.from_bytes(digest[:8], "big", signed=True)
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    same_source = WardrobeItem.original_image_url == source
    if owned_key:
        same_source = func.split_part(WardrobeItem.original_image_url, "?", 1).endswith(
            f"/{owned_key}", autoescape=True
        )
    if fingerprint:
        sibling_sources = select(ImportTask.input_payload["image_url"].astext).where(
            ImportTask.user_id == user_id,
            ImportTask.input_payload["photo_fingerprint"].astext == fingerprint,
        )
        same_source = or_(same_source, WardrobeItem.original_image_url.in_(sibling_sources))
    existing = await session.scalars(
        select(WardrobeItem).where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.deleted_at.is_(None),
            WardrobeItem.category == confirmed_category,
            same_source,
        )
    )
    matches = [
        item for item in existing
        if _identity(item.category, item.canonical_name, item.title) == identity
    ]
    return matches[0] if len(matches) == 1 else None
