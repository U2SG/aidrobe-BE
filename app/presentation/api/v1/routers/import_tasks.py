import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, status
from redis.asyncio import Redis
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.avatar.enqueue_candidate_detection import enqueue_candidate_detection
from app.application.import_tasks.confirm_import import (
    ConfirmImageImportTask,
    ConfirmItemInput,
    confirmed_item_ids,
)
from app.application.import_tasks.create_import import CreateImageImportTask
from app.application.purchase.garment_type import garment_type
from app.common import error_codes
from app.common.enums import AvatarSourceType, ImportStatus, InputType
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.cache.client import get_redis_client, invalidate_daily_recommendation_cache
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.import_task_repo import (
    SqlAlchemyImportTaskRepository,
)
from app.infrastructure.persistence.repositories.operation_log_repo import (
    SqlAlchemyOperationLogRepository,
)
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository
from app.infrastructure.persistence.session import get_db_session
from app.infrastructure.storage.object_key import extract_internal_object_key, is_owned_object_key
from app.infrastructure.workers.import_worker import enqueue_import_task, process_import_task_with_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.render_url import resolve_render_url
from app.presentation.api.v1.schemas.common import ResponseModel
from app.presentation.api.v1.schemas.import_tasks import (
    CandidateItem,
    ConfirmedItem,
    ConfirmItemsRequest,
    ConfirmItemsResponse,
    CreateImportTaskRequest,
    ImportDuplicateHint,
    ImportTaskResponse,
)
from app.presentation.api.v1.schemas.wardrobe import ConfirmImportRequest, ConfirmImportResponse

_RECEIPT_INPUT_TYPES = {
    InputType.RECEIPT_SCREENSHOT.value,
    InputType.ORDER_SCREENSHOT.value,
    InputType.SHOPPING_EMAIL.value,
}

# Recognition can return a blue shade or the broader hue for the same garment.
_BLUE_SHADES = frozenset({"blue", "light_blue", "light blue", "dark_blue", "dark blue"})

router = APIRouter(prefix="/v1/import-tasks", tags=["import-tasks"])


def to_response(task: ImportTask) -> ImportTaskResponse:
    candidate_items = _candidate_items_from_payload(task.candidate_payload, confirmed_item_ids(task))
    # Compat: `candidate_item` mirrors the first recommended candidate
    # (`should_import`), falling back to the first one overall.
    candidate_item = next(
        (item for item in candidate_items if item.should_import),
        candidate_items[0] if candidate_items else None,
    )
    return ImportTaskResponse(
        task_id=task.id,
        status=task.task_status,
        candidate_item=candidate_item,
        candidate_items=candidate_items,
        error_code=task.error_code,
        error_message=task.error_message,
    )


def _candidate_items_from_payload(
    payload: dict[str, object] | None, confirmed: dict[str, str] | None = None
) -> list[CandidateItem]:
    if not payload:
        return []
    # BE#24 — new multi-candidate payloads nest the list under `candidate_items`.
    raw_items = payload.get("candidate_items")
    if isinstance(raw_items, list):
        candidates = [CandidateItem(**item) for item in raw_items]
        for candidate in candidates:
            if candidate.temp_item_id and confirmed and candidate.temp_item_id in confirmed:
                candidate.confirmed_item_id = uuid.UUID(confirmed[candidate.temp_item_id])
        return candidates
    # Legacy / OCR payloads store a single candidate at the top level; the
    # nested key is absent, so map the top-level shape to a length-1 list.
    legacy = {key: value for key, value in payload.items() if key != "candidate_items"}
    return [CandidateItem(**legacy)]


def _image_for_review(url: str | None, user_id: uuid.UUID) -> str | None:
    key = extract_internal_object_key(url, get_settings())
    if key is not None and is_owned_object_key(key, user_id):
        return resolve_render_url(key, url)
    return url


def _review_photos(response: ImportTaskResponse, user_id: uuid.UUID) -> None:
    for candidate in response.candidate_items:
        candidate.original_image_url = _image_for_review(candidate.original_image_url, user_id) or (
            candidate.original_image_url
        )
        candidate.crop_image_url = _image_for_review(candidate.crop_image_url, user_id)
        candidate.cutout_image_url = _image_for_review(candidate.cutout_image_url, user_id)


async def _attach_duplicates(
    response: ImportTaskResponse, session: AsyncSession, user_id: uuid.UUID
) -> None:
    pending = [candidate for candidate in response.candidate_items if candidate.confirmed_item_id is None]
    matching_colors = {
        candidate.color_main: _BLUE_SHADES if candidate.color_main in _BLUE_SHADES else {candidate.color_main}
        for candidate in pending
    }
    category_colors = {
        (candidate.category, color) for candidate in pending for color in matching_colors[candidate.color_main]
    }
    if not category_colors:
        return

    matching = await session.scalars(
        select(WardrobeItem)
        .where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.deleted_at.is_(None),
            tuple_(WardrobeItem.category, WardrobeItem.color_main).in_(category_colors),
        )
        .order_by(WardrobeItem.created_at.desc(), WardrobeItem.id.desc())
    )
    items = list(matching)
    for candidate in pending:
        same_type = garment_type(candidate.canonical_name, candidate.title)
        if same_type is None:
            continue
        matches = [
            item for item in items
            if item.category == candidate.category
            and item.color_main in matching_colors[candidate.color_main]
            and garment_type(item.canonical_name, item.title) == same_type
        ]
        candidate.possible_duplicates = [
            ImportDuplicateHint(
                item_id=item.id, title=item.title,
                image_url=_image_for_review(item.cover_image_url or item.original_image_url, user_id),
            )
            for item in matches[:3]
        ]


@router.get("", response_model=ResponseModel[list[ImportTaskResponse]])
async def list_pending_import_tasks(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ResponseModel[list[ImportTaskResponse]]:
    """List this account's recognised imports that still need review."""
    tasks = await SqlAlchemyImportTaskRepository(session).list_pending(current_user.id, limit=limit, offset=offset)
    results = [to_response(task) for task in tasks]
    for result in results:
        _review_photos(result, current_user.id)
    return ResponseModel(data=results)


@router.post(
    "",
    response_model=ResponseModel[ImportTaskResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_import_task(
    payload: CreateImportTaskRequest,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> ResponseModel[ImportTaskResponse]:
    if payload.image_url is None and payload.source_url is None:
        raise FriendlyException(
            error_codes.VALIDATION_ERROR,
            "image_url or source_url is required",
            status_code=422,
        )

    # M18 — receipt / order / email input_types require the feature flag.
    if payload.input_type in _RECEIPT_INPUT_TYPES and not get_settings().ENABLE_RECEIPT_IMPORT:
        raise FriendlyException(
            error_codes.VALIDATION_ERROR,
            "receipt import is not enabled",
            status_code=400,
        )

    repository = SqlAlchemyImportTaskRepository(session)
    use_case = CreateImageImportTask(repository)
    task, created = await use_case.execute(
        user_id=current_user.id,
        input_type=payload.input_type,
        source_type=payload.source_type,
        image_url=payload.image_url,
        source_url=payload.source_url,
        locale=payload.locale,
        idempotency_key=idempotency_key,
    )
    response = to_response(task)
    if not created:
        # Retried create with the same Idempotency-Key: the first attempt
        # already queued processing.
        return ResponseModel(data=response)
    # BE#26 — when enabled, hand processing to job_runtime/arq (own session,
    # lease/retry/dead-letter) instead of running heavy Vision/OCR/link work in
    # the request lifecycle on the request's session. The import_tasks row is
    # already persisted, so the job (or legacy path) only drives processing.
    if get_settings().ENABLE_IMPORT_ASYNC_JOBS:
        await enqueue_import_task(session, task_id=task.id, user_id=current_user.id)
    else:
        background_tasks.add_task(process_import_task_with_session, session, task.id, current_user.id)
    return ResponseModel(data=response)


@router.post("/{task_id}/confirm", response_model=ResponseModel[ConfirmImportResponse])
async def confirm_import_task(
    task_id: uuid.UUID,
    payload: ConfirmImportRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
) -> ResponseModel[ConfirmImportResponse]:
    use_case = ConfirmImageImportTask(
        SqlAlchemyImportTaskRepository(session),
        SqlAlchemyWardrobeRepository(session),
        SqlAlchemyRecommendationRepository(session),
        SqlAlchemyStyleRepository(session),
    )
    confirmed = await use_case.execute(
        task_id=task_id,
        user_id=current_user.id,
        title=payload.title,
        category=payload.category,
        color_main=payload.color_main,
        location=payload.location,
        size_label=payload.size_label,
        fit_status=payload.fit_status,
        fit_intent=payload.fit_intent.value if payload.fit_intent else None,
        purchase_price=payload.purchase_price,
        purchase_date=payload.purchase_date,
    )
    item = confirmed.item
    if confirmed.created:
        await SqlAlchemyOperationLogRepository(session).add(
            current_user.id,
            "item_imported",
            {"category": item.category, "color_main": item.color_main},
        )
        await invalidate_daily_recommendation_cache(redis_client, current_user.id)
    return ResponseModel(data=ConfirmImportResponse(item_id=item.id))


@router.post("/{task_id}/confirm-items", response_model=ResponseModel[ConfirmItemsResponse])
async def confirm_import_items(
    task_id: uuid.UUID,
    payload: ConfirmItemsRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
) -> ResponseModel[ConfirmItemsResponse]:
    use_case = ConfirmImageImportTask(
        SqlAlchemyImportTaskRepository(session),
        SqlAlchemyWardrobeRepository(session),
        SqlAlchemyRecommendationRepository(session),
        SqlAlchemyStyleRepository(session),
    )
    confirmed = await use_case.confirm_items(
        task_id=task_id,
        user_id=current_user.id,
        items=[
            ConfirmItemInput(
                temp_item_id=item.temp_item_id,
                title=item.title,
                category=item.category,
                color_main=item.color_main,
                location=item.location,
                size_label=item.size_label,
                fit_status=item.fit_status,
                fit_intent=item.fit_intent.value if item.fit_intent else None,
            )
            for item in payload.items
        ],
        dismiss_remaining=payload.dismiss_remaining,
    )
    operation_log_repo = SqlAlchemyOperationLogRepository(session)
    new_entries = [entry for entry in confirmed if entry.created]
    for entry in new_entries:
        await operation_log_repo.add(
            current_user.id,
            "item_imported",
            {"category": entry.item.category, "color_main": entry.item.color_main},
        )
    if new_entries:
        await invalidate_daily_recommendation_cache(redis_client, current_user.id)
        await _offer_outfit_import_as_avatar_candidate(session, task_id=task_id, user_id=current_user.id)
    task = await SqlAlchemyImportTaskRepository(session).get_for_user(task_id, current_user.id)
    return ResponseModel(
        data=ConfirmItemsResponse(
            created_items=[
                ConfirmedItem(temp_item_id=entry.temp_item_id, item_id=entry.item.id) for entry in confirmed
            ],
            task_status=task.task_status if task else None,
        )
    )


async def _offer_outfit_import_as_avatar_candidate(
    session: AsyncSession, *, task_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """V3-012 source B (V3_SPEC §7.1): the full-body photo a user uploaded to
    split into garments may also be reusable person reference material.

    Hooked here, after the garments are confirmed, because §7.1 requires the
    person-reference question to come **after** the clothing import completes and
    to be authorized separately — importing clothes never implies consent
    (contract §4.3). Queue-only: the enqueue contains its own failures, so a
    detection problem cannot fail an import the user has already completed.

    Filtered to `input_type == outfit_image` — that is the one input type whose
    subject is a person wearing an outfit. `image`/`screenshot`/`receipt_*` are
    single garments, product pages and receipts; running human segmentation over
    a taobao screenshot would burn CPU to learn what the input type already
    tells us. The rule layer would reject a flat-lay anyway, but it would have to
    fetch and segment the photo to do it, and a rejected row would then block
    that key forever via `one_row_per_source_object`.

    The flag is read here rather than left to `enqueue_candidate_detection`,
    which also checks it: reading the import task back costs a SELECT on every
    confirm-items call, and a feature that is switched off must not charge the
    import flow for existing.
    """
    settings = get_settings()
    if not settings.ENABLE_V3_AVATAR:
        return
    task = await SqlAlchemyImportTaskRepository(session).get_for_user(task_id, user_id)
    if task is None or task.input_type != InputType.OUTFIT_IMAGE.value:
        return
    image_url = (task.input_payload or {}).get("image_url")
    if not isinstance(image_url, str):
        return
    correlation_id = structlog.contextvars.get_contextvars().get("correlation_id")
    await enqueue_candidate_detection(
        session,
        user_id=user_id,
        source_type=AvatarSourceType.OUTFIT_IMPORT,
        image_url=image_url,
        settings=settings,
        correlation_id=correlation_id if isinstance(correlation_id, str) else None,
    )


@router.get("/{task_id}", response_model=ResponseModel[ImportTaskResponse])
async def get_import_task(
    task_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[ImportTaskResponse]:
    repository = SqlAlchemyImportTaskRepository(session)
    task = await repository.get_for_user(task_id, current_user.id)
    if task is None:
        raise FriendlyException(
            error_codes.NOT_FOUND,
            "Import task not found",
            status_code=404,
        )
    result = to_response(task)
    _review_photos(result, current_user.id)
    if task.task_status == ImportStatus.WAITING_CONFIRM.value:
        await _attach_duplicates(result, session, current_user.id)
    return ResponseModel(data=result)
