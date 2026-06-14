import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.style.add_inspiration import AddStyleInspiration
from app.application.style.add_inspiration_batch import AddStyleInspirationBatch
from app.application.style.add_pdf_source import AddPdfStyleSource
from app.application.style.delete_inspiration import DeleteStyleInspiration
from app.application.style.list_inspirations import ListStyleInspirations
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import Settings, get_settings
from app.infrastructure.cache.client import get_redis_client, invalidate_daily_recommendation_cache
from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository
from app.infrastructure.persistence.session import get_db_session
from app.infrastructure.workers.pdf_worker import enqueue_pdf_ingest
from app.infrastructure.workers.style_learn_worker import learn_style_source
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.common import ResponseModel
from app.presentation.api.v1.schemas.style import (
    AddInspirationRequest,
    BatchAddInspirationRequest,
    BatchInspirationResponse,
    InspirationListResponse,
    InspirationResponse,
    PdfStyleSourceRequest,
    PdfStyleSourceResponse,
    UpdateStyleTagsRequest,
)

router = APIRouter(prefix="/v1/style", tags=["style"])


def _inspiration_response(profile: UserStyleProfile) -> InspirationResponse:
    return InspirationResponse(
        id=profile.id,
        image_url=profile.image_url,
        source_url=profile.source_url,
        source_type=profile.source_type,
        learn_status=profile.learn_status,
        created_at=profile.created_at,
    )


@router.post(
    "/inspirations",
    response_model=ResponseModel[InspirationResponse],
    status_code=201,
)
async def add_inspiration(
    payload: AddInspirationRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
    background_tasks: BackgroundTasks,
) -> ResponseModel[InspirationResponse]:
    use_case = AddStyleInspiration(SqlAlchemyStyleRepository(session))
    profile = await use_case.execute(
        current_user.id,
        image_url=payload.image_url,
        source_url=payload.source_url,
    )
    background_tasks.add_task(learn_style_source, None, str(profile.id))
    background_tasks.add_task(
        invalidate_daily_recommendation_cache, redis_client, current_user.id
    )
    return ResponseModel(data=_inspiration_response(profile))


@router.post(
    "/sources/batch",
    response_model=ResponseModel[BatchInspirationResponse],
    status_code=201,
)
async def batch_add_inspirations(
    payload: BatchAddInspirationRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
    background_tasks: BackgroundTasks,
) -> ResponseModel[BatchInspirationResponse]:
    use_case = AddStyleInspirationBatch(SqlAlchemyStyleRepository(session))
    profiles = await use_case.execute(current_user.id, payload.urls)
    for profile in profiles:
        background_tasks.add_task(learn_style_source, None, str(profile.id))
    background_tasks.add_task(
        invalidate_daily_recommendation_cache, redis_client, current_user.id
    )
    return ResponseModel(
        data=BatchInspirationResponse(items=[_inspiration_response(p) for p in profiles])
    )


@router.post(
    "/sources",
    response_model=ResponseModel[PdfStyleSourceResponse],
    status_code=201,
)
async def add_pdf_source(
    payload: PdfStyleSourceRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ResponseModel[PdfStyleSourceResponse]:
    """M19 — async PDF / magazine multi-page style ingest.

    `ENABLE_PDF_STYLE_INGEST` is an upload-ingest kill switch (default off).
    Flag=off → gate-and-reject (no row created). Convention: data-always-persists
    applies to user-triggered flags (ENABLE_TODAY_REFRESH etc.), not to this
    upload-ingest kill-switch where creating a row with no processing is meaningless.
    `estimated_pages` is best-effort 0 at enqueue: the real count is only known
    once the worker downloads and rasterizes the PDF, and blocking the request on
    an OSS fetch would defeat the async design.
    """
    if not settings.ENABLE_PDF_STYLE_INGEST:
        raise FriendlyException(
            error_codes.FORBIDDEN,
            "pdf style ingest is not enabled",
            status_code=503,
        )

    use_case = AddPdfStyleSource(SqlAlchemyStyleRepository(session))
    profile = await use_case.execute(current_user.id, file_url=payload.file_url)
    await enqueue_pdf_ingest(
        session,
        parent_id=profile.id,
        file_url=payload.file_url,
        user_id=current_user.id,
    )
    # No explicit commit — get_db_session commits on yield-exit.
    return ResponseModel(
        data=PdfStyleSourceResponse(source_id=profile.id, status="queued", estimated_pages=0)
    )


@router.get(
    "/inspirations",
    response_model=ResponseModel[InspirationListResponse],
)
async def list_inspirations(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ResponseModel[InspirationListResponse]:
    use_case = ListStyleInspirations(SqlAlchemyStyleRepository(session))
    profiles = await use_case.execute(current_user.id, limit, offset)
    return ResponseModel(
        data=InspirationListResponse(
            items=[_inspiration_response(p) for p in profiles],
            limit=limit,
            offset=offset,
        )
    )


@router.delete(
    "/inspirations/{inspiration_id}",
    response_model=ResponseModel[None],
)
async def delete_inspiration(
    inspiration_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
    background_tasks: BackgroundTasks,
) -> ResponseModel[None]:
    use_case = DeleteStyleInspiration(SqlAlchemyStyleRepository(session))
    await use_case.execute(inspiration_id, current_user.id)
    background_tasks.add_task(
        invalidate_daily_recommendation_cache, redis_client, current_user.id
    )
    return ResponseModel(data=None)


@router.patch(
    "/tags",
    response_model=ResponseModel[None],
)
async def update_style_tags(
    payload: UpdateStyleTagsRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
    background_tasks: BackgroundTasks,
) -> ResponseModel[None]:
    repo = SqlAlchemyStyleRepository(session)
    await repo.upsert_style_tags(current_user.id, payload.tags)
    background_tasks.add_task(
        invalidate_daily_recommendation_cache, redis_client, current_user.id
    )
    return ResponseModel(data=None)
