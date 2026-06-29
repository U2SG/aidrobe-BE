"""Travel Packing List router — M16. Off by default; V3-style direct return.

Rule-based selection from the real wardrobe (no LLM). A list is generated for a
trip (destination + days + scenes), stored as draft, and editable until the
user confirms. The flag only gates exposure — lists persist regardless.
"""

import uuid
from typing import Annotated, cast

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.packing.generate import (
    GeneratePackingList,
    GetPackingList,
    ListPackingLists,
    UpdatePackingList,
)
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.packing_list import PackingList
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.packing_list_repo import (
    SqlAlchemyPackingListRepository,
)
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.packing_list import (
    CreatePackingListRequest,
    PackingItem,
    PackingListResponse,
    PackingPlan,
    UpdatePackingListRequest,
)

router = APIRouter(prefix="/v1/packing-lists", tags=["packing-list"])


def _gate() -> None:
    if not get_settings().ENABLE_PACKING_LIST:
        raise FriendlyException(
            error_codes.FORBIDDEN, "Packing list is not enabled", status_code=503
        )


def _to_response(packing_list: PackingList) -> PackingListResponse:
    raw_items = cast(list[dict[str, object]], packing_list.items or [])
    raw_scenes = cast(list[str], packing_list.scenes or [])
    plan_json = cast("dict[str, object] | None", packing_list.plan_json)
    return PackingListResponse(
        packing_list_id=packing_list.id,
        destination=packing_list.destination,
        days=packing_list.days,
        scenes=list(raw_scenes),
        departure_date=packing_list.departure_date,
        items=[PackingItem.model_validate(item) for item in raw_items],
        status=packing_list.status,
        plan=PackingPlan.model_validate(plan_json) if plan_json else None,
    )


@router.post("", response_model=PackingListResponse)
async def create_packing_list(
    payload: CreatePackingListRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PackingListResponse:
    _gate()
    repo = SqlAlchemyPackingListRepository(session)
    packing_list = await GeneratePackingList(repo).execute(
        current_user.id,
        payload.destination,
        payload.days,
        payload.scenes,
        payload.weather_hint,
        payload.departure_date,
    )
    return _to_response(packing_list)


@router.get("", response_model=list[PackingListResponse])
async def list_packing_lists(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[PackingListResponse]:
    _gate()
    repo = SqlAlchemyPackingListRepository(session)
    lists = await ListPackingLists(repo).execute(current_user.id)
    return [_to_response(pl) for pl in lists]


@router.get("/{packing_list_id}", response_model=PackingListResponse)
async def get_packing_list(
    packing_list_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PackingListResponse:
    _gate()
    repo = SqlAlchemyPackingListRepository(session)
    packing_list = await GetPackingList(repo).execute(packing_list_id, current_user.id)
    return _to_response(packing_list)


@router.patch("/{packing_list_id}", response_model=PackingListResponse)
async def update_packing_list(
    packing_list_id: uuid.UUID,
    payload: UpdatePackingListRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PackingListResponse:
    _gate()
    repo = SqlAlchemyPackingListRepository(session)
    items = (
        [item.model_dump(mode="json") for item in payload.items]
        if payload.items is not None
        else None
    )
    packing_list = await UpdatePackingList(repo).execute(
        packing_list_id,
        current_user.id,
        items,
        payload.status,
    )
    return _to_response(packing_list)
