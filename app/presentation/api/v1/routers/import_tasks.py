import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.import_tasks.confirm_import import ConfirmImageImportTask
from app.application.import_tasks.create_import import CreateImageImportTask
from app.common import error_codes
from app.common.enums import InputType
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.cache.client import get_redis_client, invalidate_daily_recommendation_cache
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.user import User
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
from app.infrastructure.workers.import_worker import process_import_task_with_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.common import ResponseModel
from app.presentation.api.v1.schemas.import_tasks import (
    CandidateItem,
    CreateImportTaskRequest,
    ImportTaskResponse,
)
from app.presentation.api.v1.schemas.wardrobe import ConfirmImportRequest, ConfirmImportResponse

_RECEIPT_INPUT_TYPES = {
    InputType.RECEIPT_SCREENSHOT.value,
    InputType.ORDER_SCREENSHOT.value,
    InputType.SHOPPING_EMAIL.value,
}

router = APIRouter(prefix="/v1/import-tasks", tags=["import-tasks"])


def to_response(task: ImportTask) -> ImportTaskResponse:
    candidate_item = None
    if task.candidate_payload is not None:
        candidate_item = CandidateItem(**task.candidate_payload)
    return ImportTaskResponse(
        task_id=task.id,
        status=task.task_status,
        candidate_item=candidate_item,
        error_code=task.error_code,
        error_message=task.error_message,
    )


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
    task = await use_case.execute(
        user_id=current_user.id,
        input_type=payload.input_type,
        source_type=payload.source_type,
        image_url=payload.image_url,
        source_url=payload.source_url,
        locale=payload.locale,
    )
    response = to_response(task)
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
    item = await use_case.execute(
        task_id=task_id,
        user_id=current_user.id,
        title=payload.title,
        category=payload.category,
        color_main=payload.color_main,
        location=payload.location,
        size_label=payload.size_label,
        fit_status=payload.fit_status,
        purchase_price=payload.purchase_price,
        purchase_date=payload.purchase_date,
    )
    await SqlAlchemyOperationLogRepository(session).add(
        current_user.id,
        "item_imported",
        {"category": item.category, "color_main": item.color_main},
    )
    await invalidate_daily_recommendation_cache(redis_client, current_user.id)
    return ResponseModel(data=ConfirmImportResponse(item_id=item.id))


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
    return ResponseModel(data=to_response(task))
