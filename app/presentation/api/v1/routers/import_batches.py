"""Album scan: a batch of photo imports reviewed per garment instead of per photo."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.import_tasks.batch_groups import BatchCandidate, GarmentImage
from app.application.import_tasks.confirm_batch import ConfirmImportBatch, GroupDecision
from app.application.import_tasks.import_batch import BatchView, ImageLoader, load_batch_view
from app.application.ports.vision import VisionPort
from app.bootstrap.container import vision_service
from app.infrastructure.cache.client import get_redis_client, invalidate_daily_recommendation_cache
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.operation_log_repo import SqlAlchemyOperationLogRepository
from app.infrastructure.persistence.session import get_db_session
from app.infrastructure.storage.source_image import load_source_bytes
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.render_url import resolve_render_url, wardrobe_image_urls
from app.presentation.api.v1.schemas.common import ResponseModel
from app.presentation.api.v1.schemas.import_tasks import (
    ConfirmImportBatchRequest,
    ConfirmImportBatchResponse,
    ImportBatchExistingItem,
    ImportBatchGroup,
    ImportBatchGroupResult,
    ImportBatchMember,
    ImportBatchResponse,
)

router = APIRouter(prefix="/v1/import-batches", tags=["import-tasks"])


def get_vision_service() -> VisionPort:
    return vision_service()


async def _load_image(image: GarmentImage) -> bytes:
    # Refs come from our own crop pipeline / wardrobe rows, so keys are trusted.
    return await load_source_bytes(image.url, image.key)


def get_image_loader() -> ImageLoader:
    return _load_image


def _render(image: GarmentImage | None) -> str | None:
    return resolve_render_url(image.key, image.url) if image is not None else None


async def _to_response(session: AsyncSession, view: BatchView) -> ImportBatchResponse:
    photos = {
        task.id: GarmentImage(
            key=(task.input_payload or {}).get("source_object_key"), url=(task.input_payload or {}).get("image_url"),
        )
        for task in view.tasks
    }
    items = [group.existing_item for group in view.groups if group.existing_item is not None]
    images = await wardrobe_image_urls(session, items)

    def member(candidate: BatchCandidate) -> ImportBatchMember:
        return ImportBatchMember(
            task_id=candidate.task_id, temp_item_id=candidate.temp_item_id, crop_url=_render(candidate.image),
            photo_url=_render(photos.get(candidate.task_id)), taken_at=candidate.taken_at,
        )

    groups = []
    for group in view.groups:
        item = group.existing_item
        groups.append(
            ImportBatchGroup(
                group_id=group.group_id, kind=group.kind, status=group.status,
                category=group.best.category, color_main=group.best.color_main, title=group.best.title,
                best_crop_url=_render(group.best.image),
                members=[member(candidate) for candidate in group.members],
                existing_item=ImportBatchExistingItem(
                    item_id=item.id, title=item.title, image_url=images[item.id].cover or images[item.id].original,
                ) if item is not None else None,
                wear_dates=sorted({m.taken_at for m in group.members if m.taken_at is not None}),
            )
        )
    return ImportBatchResponse(
        batch_id=view.batch_id, total_photos=view.total_photos, processing=view.processing, failed=view.failed,
        complete=view.complete, groups=groups,
    )


@router.get("/{batch_id}", response_model=ResponseModel[ImportBatchResponse])
async def get_import_batch(
    batch_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    vision: Annotated[VisionPort, Depends(get_vision_service)],
    load_image: Annotated[ImageLoader, Depends(get_image_loader)],
) -> ResponseModel[ImportBatchResponse]:
    """Garments found across the batch's photos. Poll while `complete` is false."""
    view = await load_batch_view(session, current_user.id, batch_id, vision, load_image, release_connection=True)
    return ResponseModel(data=await _to_response(session, view))


@router.post("/{batch_id}/confirm", response_model=ResponseModel[ConfirmImportBatchResponse])
async def confirm_import_batch(
    batch_id: uuid.UUID,
    payload: ConfirmImportBatchRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
    vision: Annotated[VisionPort, Depends(get_vision_service)],
    load_image: Annotated[ImageLoader, Depends(get_image_loader)],
) -> ResponseModel[ConfirmImportBatchResponse]:
    """Apply create / link / skip per group. Safe to retry: replays return the same items."""
    outcomes = await ConfirmImportBatch(session, vision, load_image).execute(
        current_user.id, batch_id,
        [
            GroupDecision(
                group_id=decision.group_id, action=decision.action, title=decision.title,
                category=decision.category, color_main=decision.color_main, backfill_wear=decision.backfill_wear,
            )
            for decision in payload.groups
        ],
    )
    operation_log_repo = SqlAlchemyOperationLogRepository(session)
    for outcome in outcomes:
        if outcome.created and outcome.item is not None:
            await operation_log_repo.add(
                current_user.id, "item_imported",
                {"category": outcome.item.category, "color_main": outcome.item.color_main},
            )
    if any(outcome.created or outcome.wears_recorded for outcome in outcomes):
        await invalidate_daily_recommendation_cache(redis_client, current_user.id)
    return ResponseModel(
        data=ConfirmImportBatchResponse(
            groups=[
                ImportBatchGroupResult(
                    group_id=outcome.group_id, action=outcome.action,
                    item_id=outcome.item.id if outcome.item is not None else None,
                    created=outcome.created, wears_recorded=outcome.wears_recorded,
                )
                for outcome in outcomes
            ]
        )
    )
