"""Find one garment's reference for recognition and product photography."""

import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.vision import VisionPort
from app.application.purchase.garment_type import garment_type
from app.common import error_codes
from app.common.enums import ImageType
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.storage.object_key import is_legacy_garment_crop, is_owned_object_key, trusted_item_image_key
from app.infrastructure.storage.source_image import load_source_bytes, resolve_import_source_key
from app.infrastructure.workers.garment_crops import crop_regions


@dataclass(frozen=True)
class ItemReference:
    url: str
    key: str | None
    crop: bytes | None = None


async def item_reference(
    session: AsyncSession, item: WardrobeItem, vision: VisionPort, locale: str,
) -> ItemReference:
    original = item.original_image_url
    if not original:
        raise FriendlyException(error_codes.VALIDATION_ERROR, "没有可用的衣物原照", status_code=422)
    images = await session.scalars(select(WardrobeItemImage).where(
        WardrobeItemImage.item_id == item.id,
        WardrobeItemImage.image_type.in_([ImageType.CROP.value, ImageType.STANDARD.value]),
    ).order_by(WardrobeItemImage.created_at.desc(), WardrobeItemImage.id))
    settings = get_settings()
    for image in images:
        if (
            image.object_key
            and trusted_item_image_key(image.image_url, item.user_id, settings) == image.object_key
            and is_owned_object_key(image.object_key, item.user_id)
            and (image.image_type == ImageType.CROP.value or "/wardrobe/verified-crops/" in image.object_key)
            and not is_legacy_garment_crop(image.object_key, item.user_id)
        ):
            return ItemReference(image.image_url, image.object_key)

    cutout = await session.scalar(
        select(AsyncJob)
        .where(AsyncJob.job_type == "cutout", AsyncJob.target_id == item.id, AsyncJob.user_id == item.user_id)
        .order_by(AsyncJob.created_at.desc(), AsyncJob.id.desc())
        .limit(1)
    )
    saved = cutout.payload if cutout else None
    if saved:
        saved_key = saved.get("source_key")
        saved_url = saved.get("source_url")
        if (
            isinstance(saved_key, str) and isinstance(saved_url, str)
            and is_owned_object_key(saved_key, item.user_id)
            and trusted_item_image_key(saved_url, item.user_id, settings) == saved_key
            and not is_legacy_garment_crop(saved_key, item.user_id)
        ):
            return ItemReference(saved_url, saved_key)

    key = resolve_import_source_key(original, item.user_id, settings)
    # Some older items kept only the full outfit photo. Locate the intended
    # garment again before passing anything to single-item recognition or generation.
    outfit_import = await session.scalar(select(ImportTask.id).where(
        ImportTask.user_id == item.user_id,
        ImportTask.input_type == "outfit_image",
        ImportTask.input_payload["image_url"].as_string() == original,
    ).limit(1))
    if outfit_import is None:
        return ItemReference(original, key)

    candidates = [
        result for result in await vision.analyze_outfit(original, locale)
        if result.category == item.category and result.bbox and not result.style_unverified
    ]
    kind = garment_type(item.title, item.canonical_name)
    matches = [
        result for result in candidates if kind and garment_type(result.title, result.canonical_name) == kind
    ]
    selected = matches or candidates
    if len(selected) == 1:
        raw = await load_source_bytes(original, key, user_id=item.user_id)
        crop = (await asyncio.to_thread(crop_regions, raw, [selected[0].bbox]))[0]
        if crop and await vision.review_garment_crops([crop], selected) == [True]:
            return ItemReference(original, key, crop)
    raise FriendlyException(error_codes.VALIDATION_ERROR, "原照里无法单独确认这件衣物", status_code=422)
