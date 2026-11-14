"""Outfit visual assets router — M10."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast
from urllib.parse import urlsplit

import structlog
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cost_guard import assert_within_quota
from app.common import error_codes
from app.common.enums import CutoutStatus, VisualType
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.cache.client import get_redis_client
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.recommendation import OutfitItem
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.repositories.outfit_visual_repo import (
    SqlAlchemyOutfitVisualRepository,
)
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.session import get_db_session as get_db
from app.infrastructure.storage.object_key import is_legacy_garment_crop, trusted_item_image_key
from app.infrastructure.workers.job_runtime import JOB_STATUS_SUCCEEDED
from app.infrastructure.workers.outfit_board_worker import enqueue_outfit_board
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.render_url import resolve_render_url as _resolve_render_url

router = APIRouter(prefix="/v1", tags=["outfit-visual"])
logger = structlog.get_logger(__name__)

# A job that has been queued this long with no worker claims it is effectively
# stale — surface 'failed' so the frontend stops polling instead of looping
# until the user navigates away.
_STALE_QUEUED_MINUTES = 15
# v2: used slots are centred as a group (v1 left blank bands for missing roles).
_OUTFIT_BOARD_LAYOUT_VERSION = "v2"
_GENERATED_OUTFIT_LAYOUT_VERSION = "gpt-reviewed-v7"
_IMAGE_TYPE_PRIORITY = ("cutout", "thumbnail", "standard", "crop", "original")

_REFERENCE_FIELDS = (
    "review_object_key", "review_image_url", "cutout_object_key", "cutout_image_url",
    "thumbnail_object_key", "thumbnail_url",
)


class OutfitVisualAssetResponse(BaseModel):
    asset_id: uuid.UUID
    generation_status: str
    image_url: str | None = None
    visual_type: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    # V3-019 — contract §6.2's two ADDITIVE nullable fields, completing the
    # frozen GET shape now that the try-on lifecycle is exposed. `expires_at`
    # is always null in P0: the row has no retention clock (no column), and
    # §13.5's 30-day bucket rule is storage configuration this response must
    # not claim on the row's behalf — a stated null is honest, a computed
    # timestamp would assert a purge nothing here verifies. V3-021's retention
    # work owns making it real.
    created_at: datetime | None = None
    expires_at: datetime | None = None

    model_config = {"from_attributes": True}


class WardrobeItemImagesResponse(BaseModel):
    item_id: uuid.UUID
    images: list[dict[str, Any]]


def _response_for_asset(asset: OutfitVisualAsset) -> OutfitVisualAssetResponse:
    """Keep producer responses limited to the existing pollable contract."""
    return OutfitVisualAssetResponse(
        asset_id=asset.id,
        generation_status=asset.generation_status,
    )


async def _existing_outfit_board_asset(
    db: AsyncSession, *, user_id: uuid.UUID, outfit_id: uuid.UUID, layout_version: str
) -> OutfitVisualAsset | None:
    job = await db.scalar(
        select(AsyncJob).where(AsyncJob.idempotency_key == f"outfit_board:{outfit_id}:{layout_version}")
    )
    if job is None or job.target_id is None:
        return None
    return cast(
        OutfitVisualAsset | None,
        await db.scalar(
            select(OutfitVisualAsset).where(
                OutfitVisualAsset.id == job.target_id,
                OutfitVisualAsset.user_id == user_id,
                OutfitVisualAsset.outfit_id == outfit_id,
                OutfitVisualAsset.visual_type == VisualType.OUTFIT_BOARD.value,
            )
        ),
    )


async def _outfit_board_item_list(
    db: AsyncSession, *, outfit_id: uuid.UUID, user_id: uuid.UUID, generate: bool = False
) -> list[dict[str, str]]:
    rows = (
        await db.execute(
            select(OutfitItem, WardrobeItem, WardrobeItemImage)
            .join(
                WardrobeItem,
                OutfitItem.item_id == WardrobeItem.id,
            )
            .outerjoin(WardrobeItemImage, WardrobeItemImage.item_id == WardrobeItem.id)
            .where(
                OutfitItem.outfit_id == outfit_id,
                WardrobeItem.user_id == user_id,
                WardrobeItem.deleted_at.is_(None),
            )
            .order_by(OutfitItem.display_order)
        )
    ).all()
    item_images: dict[uuid.UUID, dict[str, WardrobeItemImage]] = {}
    items: dict[uuid.UUID, WardrobeItem] = {}
    roles: dict[uuid.UUID, str] = {}
    for outfit_item, item, image in rows:
        items[item.id] = item
        roles[item.id] = outfit_item.role
        if (
            image is not None
            and not is_legacy_garment_crop(image.object_key, user_id)
            and not is_legacy_garment_crop(image.image_url, user_id)
            and image.image_type not in item_images.setdefault(item.id, {})
        ):
            item_images[item.id][image.image_type] = image

    review_sources = {}
    if generate and items:
        sources = await db.execute(
            select(AsyncJob.target_id, AsyncJob.payload, AsyncJob.status, AsyncJob.result).where(
                AsyncJob.job_type == "cutout",
                AsyncJob.user_id == user_id,
                AsyncJob.target_id.in_(items),
            ).order_by(AsyncJob.created_at, AsyncJob.id)
        )
        review_sources = {
            item_id: (payload or {}, job_status, result)
            for item_id, payload, job_status, result in sources
        }

    item_list: list[dict[str, str]] = []
    for item_id, item in items.items():
        images = item_images.get(item_id, {})
        board_item = {"role": roles[item_id], "category": item.category}
        if generate:
            board_item.update(title=item.title, color=item.color_main)
            crop = images.get("crop")
            source, job_status, result = review_sources.get(item_id, ({}, None, None))
            source_key, source_url = source.get("source_key"), source.get("source_url")
            if is_legacy_garment_crop(source_key, user_id) or is_legacy_garment_crop(source_url, user_id):
                # The old worker wrote this standard image only after the product shot passed review.
                standard = images.get("standard")
                reviewed = (
                    job_status == JOB_STATUS_SUCCEEDED
                    and result is None
                    and item.cutout_status == CutoutStatus.READY.value
                    and standard is not None
                    and standard.object_key == f"{user_id}/items/{item_id}/cutout.png"
                    and standard.image_url == item.cover_image_url
                )
                owned_key = trusted_item_image_key(source_url, user_id, get_settings()) if reviewed else None
                source_url = None
                source_key = None if source_key not in (None, owned_key) else owned_key
                if not is_legacy_garment_crop(source_key, user_id):
                    source_key = None
            if crop is not None and crop.object_key:
                board_item["review_object_key"] = crop.object_key
            elif crop is not None and crop.image_url:
                board_item["review_image_url"] = crop.image_url
            elif source_key:
                board_item["review_object_key"] = source_key
            elif source_url:
                board_item["review_image_url"] = source_url
            elif item.cutout_status == "ready":
                original = images.get("original")
                if original is not None and original.object_key:
                    board_item["review_object_key"] = original.object_key
                elif item.original_image_url:
                    board_item["review_image_url"] = item.original_image_url

        cutout = images.get("cutout")
        if cutout is not None:
            if cutout.object_key:
                board_item["cutout_object_key"] = cutout.object_key
            elif cutout.image_url:
                board_item["cutout_image_url"] = cutout.image_url
        elif (
            item.cutout_status == "ready"
            and item.cover_image_url
            and not is_legacy_garment_crop(item.cover_image_url, user_id)
        ):
            # No durable object key exists for this legacy row; retain a URL-only
            # fallback rather than snapshotting a request-time signed URL.
            board_item["cutout_image_url"] = item.cover_image_url

        thumbnail = next(
            (
                images[kind]
                for kind in _IMAGE_TYPE_PRIORITY[1:]
                if kind in images and (images[kind].object_key or images[kind].image_url)
            ),
            None,
        )
        if thumbnail is not None:
            if thumbnail.object_key:
                board_item["thumbnail_object_key"] = thumbnail.object_key
            elif thumbnail.image_url:
                board_item["thumbnail_url"] = thumbnail.image_url
        else:
            thumbnail_url = item.cover_image_url
            if not thumbnail_url or is_legacy_garment_crop(thumbnail_url, user_id):
                thumbnail_url = item.original_image_url
            if thumbnail_url and not is_legacy_garment_crop(thumbnail_url, user_id):
                board_item["thumbnail_url"] = thumbnail_url

        item_list.append(board_item)
    return item_list


def _outfit_signature(slots: list[OutfitItem]) -> list[list[str]]:
    return sorted([slot.role, str(slot.item_id)] for slot in slots)


def _individual_references(item: dict[str, Any]) -> set[str]:
    # Older snapshots lack item IDs. A stored single-item crop or product image
    # can establish identity. An outfit's full original photo can belong to several pieces.
    references = set()
    for field in _REFERENCE_FIELDS:
        value = item.get(field)
        if isinstance(value, str):
            path = urlsplit(value).path.lstrip("/")
            if "/wardrobe/crops/" in path or "/wardrobe/verified-crops/" in path or "/items/" in path:
                references.add(path)
    return references


def _matches_item_id(references: set[str], item_id: str) -> bool:
    return any(
        f"/items/{item_id}/" in path or path.rsplit("/", 1)[-1].startswith(f"{item_id}-")
        for path in references
    )


async def _reusable_generated_board(
    db: AsyncSession, *, user_id: uuid.UUID, slots: list[OutfitItem], items: list[dict[str, str]],
) -> OutfitVisualAsset | None:
    signature = _outfit_signature(slots)
    if not signature or len(items) != len(slots):
        return None
    matching_snapshots = [OutfitVisualAsset.input_snapshot["garment_slots"].contains(signature)]
    references = [_individual_references(item) for item in items]
    ids_by_role = dict(signature)
    if len(ids_by_role) == len(items):
        matching_snapshots.extend(
            OutfitVisualAsset.input_snapshot["item_list"].contains([{"role": item["role"], field: value}])
            for item, refs in zip(items, references, strict=True)
            for field in _REFERENCE_FIELDS
            if (value := item.get(field)) and urlsplit(value).path.lstrip("/") in refs
        )
    assets = await db.scalars(
        select(OutfitVisualAsset)
        .join(AsyncJob, AsyncJob.target_id == OutfitVisualAsset.id)
        .where(
            OutfitVisualAsset.user_id == user_id,
            OutfitVisualAsset.visual_type == VisualType.OUTFIT_BOARD.value,
            OutfitVisualAsset.generation_status == "completed",
            or_(OutfitVisualAsset.object_key.is_not(None), OutfitVisualAsset.image_url.is_not(None)),
            AsyncJob.job_type == "outfit_board",
            AsyncJob.user_id == user_id,
            AsyncJob.payload["layout_version"].as_string() == _GENERATED_OUTFIT_LAYOUT_VERSION,
            or_(*matching_snapshots),
        )
        .order_by(OutfitVisualAsset.created_at.desc(), OutfitVisualAsset.id.desc())
    )
    for asset in assets:
        snapshot = asset.input_snapshot or {}
        if "garment_slots" in snapshot:
            if snapshot["garment_slots"] == signature:
                return asset
            continue
        previous_items = snapshot.get("item_list")
        if not isinstance(previous_items, list) or len(previous_items) != len(items):
            continue
        previous_by_role = {
            entry["role"]: _individual_references(entry)
            for entry in previous_items if isinstance(entry, dict) and "role" in entry
        }
        if previous_by_role.keys() == ids_by_role.keys() and len(ids_by_role) == len(items) and all(
            previous_by_role.get(item["role"], set()) & refs
            or _matches_item_id(previous_by_role.get(item["role"], set()), ids_by_role[item["role"]])
            for item, refs in zip(items, references, strict=True)
        ):
            return asset
    return None


@router.get("/outfits/{outfit_id}/share-asset", response_model=OutfitVisualAssetResponse)
async def get_generated_outfit_image(
    outfit_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> OutfitVisualAssetResponse:
    """Look up an existing generated image without creating any work."""
    asset = await _existing_outfit_board_asset(
        db, user_id=current_user.id, outfit_id=outfit_id, layout_version=_GENERATED_OUTFIT_LAYOUT_VERSION
    )
    if asset is None or asset.generation_status in {"failed", "rejected_by_quality_gate"}:
        outfit = await SqlAlchemyRecommendationRepository(db).get_outfit_for_user(outfit_id, current_user.id)
        if outfit is not None:
            items = await _outfit_board_item_list(db, outfit_id=outfit_id, user_id=current_user.id, generate=True)
            asset = await _reusable_generated_board(
                db, user_id=current_user.id, slots=outfit.items, items=items,
            ) or asset
    if asset is None:
        raise FriendlyException(error_codes.NOT_FOUND, "Generated image not found", status_code=404)
    return await get_outfit_visual_asset(asset.id, db, current_user)


@router.post(
    "/outfits/{outfit_id}/share-asset",
    response_model=OutfitVisualAssetResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_outfit_share_asset(
    outfit_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
    generate: bool = False,
) -> OutfitVisualAssetResponse:
    """Create or reuse an outfit board. The opt-in generation mode uses GPT image editing."""
    outfit = await SqlAlchemyRecommendationRepository(db).get_outfit_for_user(outfit_id, current_user.id)
    if outfit is None:
        raise FriendlyException(error_codes.NOT_FOUND, "Outfit not found", status_code=404)

    layout_version = _GENERATED_OUTFIT_LAYOUT_VERSION if generate else _OUTFIT_BOARD_LAYOUT_VERSION
    existing = await _existing_outfit_board_asset(
        db, user_id=current_user.id, outfit_id=outfit_id, layout_version=layout_version
    )
    if existing is not None:
        if generate and existing.generation_status == "completed":
            return await get_outfit_visual_asset(existing.id, db, current_user)
        return _response_for_asset(existing)

    item_list = await _outfit_board_item_list(db, outfit_id=outfit_id, user_id=current_user.id, generate=generate)
    if generate:
        if not item_list or len(item_list) > 16 or len(item_list) != len(outfit.items):
            raise FriendlyException(
                error_codes.VALIDATION_ERROR, "Outfit has missing or unsupported pieces", status_code=422
            )
        reusable = await _reusable_generated_board(
            db, user_id=current_user.id, slots=outfit.items, items=item_list,
        )
        if reusable is not None:
            return await get_outfit_visual_asset(reusable.id, db, current_user)
        settings = get_settings()
        if (
            not settings.ENABLE_ITEM_CUTOUT
            or settings.IMAGE_CUTOUT_PROVIDER != "openai"
            or not settings.OPENAI_API_KEY
            or not settings.QWEN_API_KEY
        ):
            raise FriendlyException(error_codes.FORBIDDEN, "Outfit image generation is not enabled", status_code=503)
    asset = OutfitVisualAsset(
        user_id=current_user.id,
        outfit_id=outfit_id,
        visual_type=VisualType.OUTFIT_BOARD.value,
        generation_status="queued",
        input_snapshot={
            "item_list": item_list,
            **({"generate": True, "garment_slots": _outfit_signature(outfit.items)} if generate else {}),
        },
    )
    await SqlAlchemyOutfitVisualRepository(db).add(asset)
    correlation_id = structlog.contextvars.get_contextvars().get("correlation_id")
    _job, inserted = await enqueue_outfit_board(
        db,
        asset_id=asset.id,
        outfit_id=outfit_id,
        layout_version=layout_version,
        user_id=current_user.id,
        correlation_id=correlation_id if isinstance(correlation_id, str) else None,
    )
    if inserted:
        if generate:
            await assert_within_quota(db, current_user.id, "outfit_image", redis_client=get_redis_client())
        return _response_for_asset(asset)

    # The job table's unique key resolves a concurrent producer race. Discard
    # this transaction's contender asset before commit, then return the winner.
    await db.delete(asset)
    await db.flush()
    existing = await _existing_outfit_board_asset(
        db, user_id=current_user.id, outfit_id=outfit_id, layout_version=layout_version
    )
    if existing is not None:
        if generate and existing.generation_status == "completed":
            return await get_outfit_visual_asset(existing.id, db, current_user)
        return _response_for_asset(existing)
    logger.error("outfit_board_idempotency_target_missing", outfit_id=str(outfit_id))
    raise FriendlyException(error_codes.INTERNAL_ERROR, "Could not create share asset", status_code=500)


@router.get(
    "/outfit-visual-assets/{asset_id}",
    response_model=OutfitVisualAssetResponse,
)
async def get_outfit_visual_asset(
    asset_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> OutfitVisualAssetResponse:
    from app.infrastructure.persistence.repositories.outfit_visual_repo import (
        SqlAlchemyOutfitVisualRepository,
    )

    repo = SqlAlchemyOutfitVisualRepository(db)
    asset = await repo.get_by_id(asset_id)
    if asset is None or asset.user_id != current_user.id:
        raise FriendlyException(error_codes.NOT_FOUND, "Asset not found", status_code=404)

    generation_status = asset.generation_status
    error_code = asset.error_code

    # Generated assets from before the review was introduced remain available
    # as records, but clients cannot display their unreviewed images.
    if asset.visual_type == VisualType.OUTFIT_BOARD.value and (asset.input_snapshot or {}).get("generate") is True:
        job = await db.scalar(
            select(AsyncJob).where(AsyncJob.target_id == asset.id, AsyncJob.job_type == "outfit_board")
        )
        if job is None or (job.payload or {}).get("layout_version") != _GENERATED_OUTFIT_LAYOUT_VERSION:
            generation_status = "rejected_by_quality_gate"

    # `quality_check` is V3-018's §6.2 intermediate state: non-terminal, so a
    # worker that dead-letters mid-gate must surface `failed` here exactly as
    # it does from `processing` — a poller stops only on terminal states.
    if generation_status in ("queued", "processing", "quality_check"):
        generation_status, error_code = await _resolve_pending_status(
            db, asset_id, generation_status, error_code, asset.created_at
        )

    return OutfitVisualAssetResponse(
        asset_id=asset.id,
        generation_status=generation_status,
        # Phase E5 — prefer object_key + sign at request time so private
        # buckets work without a long-TTL URL baked into the row. Fall
        # back to the persisted image_url for legacy / non-OSS deploys.
        image_url=(
            None if generation_status == "rejected_by_quality_gate"
            else _resolve_render_url(asset.object_key, asset.image_url)
        ),
        visual_type=asset.visual_type,
        error_code=error_code,
        error_message=asset.error_message,
        created_at=asset.created_at,
        expires_at=None,
    )


async def _resolve_pending_status(
    db: AsyncSession,
    asset_id: uuid.UUID,
    current_status: str,
    current_error_code: str | None,
    created_at: datetime,
) -> tuple[str, str | None]:
    """Return (generation_status, error_code) for a queued/processing asset.

    Surfaces 'failed' immediately when:
    - The backing async_job is dead (worker exhausted all retries), OR
    - The asset has been queued for >15 min with no worker claiming it
      (dev environment without arq running).

    This bypasses the APScheduler-based reconciler so the frontend receives
    a terminal state without waiting for the scheduler cron."""
    from sqlalchemy import or_, select

    from app.infrastructure.persistence.models.async_job import AsyncJob

    has_dead_job = await db.scalar(
        select(AsyncJob.id).where(AsyncJob.target_id == asset_id, AsyncJob.status == "dead").limit(1)
    )
    if has_dead_job:
        return "failed", current_error_code or "generation_failed"

    if current_status == "queued" and datetime.now(UTC) - created_at > timedelta(minutes=_STALE_QUEUED_MINUTES):
        return "failed", "worker_unavailable"

    return current_status, current_error_code


@router.get(
    "/wardrobe-items/{item_id}/images",
    response_model=WardrobeItemImagesResponse,
)
async def get_item_images(
    item_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> WardrobeItemImagesResponse:
    from sqlalchemy import or_, select

    from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage

    result = await db.execute(
        select(WardrobeItem).where(
            WardrobeItem.id == item_id,
            WardrobeItem.user_id == current_user.id,
            WardrobeItem.deleted_at.is_(None),
        )
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise FriendlyException(error_codes.NOT_FOUND, "Item not found", status_code=404)

    img_result = await db.execute(select(WardrobeItemImage).where(WardrobeItemImage.item_id == item_id))
    images = [
        {
            "image_type": img.image_type,
            "image_url": _resolve_render_url(img.object_key, img.image_url),
        }
        for img in img_result.scalars().all()
    ]

    return WardrobeItemImagesResponse(item_id=item_id, images=images)
