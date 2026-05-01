import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.import_tasks.confirm_import import ConfirmImageImportTask
from app.application.import_tasks.create_import import CreateImageImportTask
from app.bootstrap.container import vision_service
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.import_task_repo import (
    SqlAlchemyImportTaskRepository,
)
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.common import ResponseModel
from app.presentation.api.v1.schemas.import_tasks import (
    CandidateItem,
    CreateImportTaskRequest,
    ImportTaskResponse,
)
from app.presentation.api.v1.schemas.wardrobe import ConfirmImportRequest, ConfirmImportResponse

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


@router.post("", response_model=ResponseModel[ImportTaskResponse])
async def create_import_task(
    payload: CreateImportTaskRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[ImportTaskResponse]:
    repository = SqlAlchemyImportTaskRepository(session)
    use_case = CreateImageImportTask(repository, vision_service())
    task = await use_case.execute(
        user_id=current_user.id,
        source_type=payload.source_type,
        image_url=payload.image_url,
    )
    return ResponseModel(data=to_response(task))


@router.post("/{task_id}/confirm", response_model=ResponseModel[ConfirmImportResponse])
async def confirm_import_task(
    task_id: uuid.UUID,
    payload: ConfirmImportRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[ConfirmImportResponse]:
    use_case = ConfirmImageImportTask(
        SqlAlchemyImportTaskRepository(session),
        SqlAlchemyWardrobeRepository(session),
    )
    item = await use_case.execute(
        task_id=task_id,
        user_id=current_user.id,
        title=payload.title,
        category=payload.category,
        color_main=payload.color_main,
    )
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
