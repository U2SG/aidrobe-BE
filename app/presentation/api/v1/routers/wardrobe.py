import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.wardrobe.delete_item import DeleteWardrobeItem
from app.application.wardrobe.list_items import ListWardrobeItems
from app.application.wardrobe.update_item import UpdateWardrobeItem
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.common import ResponseModel
from app.presentation.api.v1.schemas.wardrobe import (
    DeleteWardrobeItemResponse,
    UpdateWardrobeItemRequest,
    WardrobeItemResponse,
    WardrobeListResponse,
)

router = APIRouter(prefix="/v1/wardrobe-items", tags=["wardrobe"])


def item_response(item: WardrobeItem) -> WardrobeItemResponse:
    return WardrobeItemResponse(
        item_id=item.id,
        title=item.title,
        category=item.category,
        color_main=item.color_main,
        status=item.status,
        cover_image_url=item.cover_image_url,
        original_image_url=item.original_image_url,
    )


@router.get("", response_model=ResponseModel[WardrobeListResponse])
async def list_wardrobe_items(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    category: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ResponseModel[WardrobeListResponse]:
    use_case = ListWardrobeItems(SqlAlchemyWardrobeRepository(session))
    items, total = await use_case.execute(current_user.id, category, limit, offset)
    return ResponseModel(
        data=WardrobeListResponse(
            items=[item_response(item) for item in items],
            limit=limit,
            offset=offset,
            total=total,
        )
    )


@router.get("/{item_id}", response_model=ResponseModel[WardrobeItemResponse])
async def get_wardrobe_item(
    item_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[WardrobeItemResponse]:
    item = await SqlAlchemyWardrobeRepository(session).get_for_user(item_id, current_user.id)
    if item is None:
        raise FriendlyException(error_codes.NOT_FOUND, "Wardrobe item not found", status_code=404)
    return ResponseModel(data=item_response(item))


@router.patch("/{item_id}", response_model=ResponseModel[WardrobeItemResponse])
async def update_wardrobe_item(
    item_id: uuid.UUID,
    payload: UpdateWardrobeItemRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[WardrobeItemResponse]:
    use_case = UpdateWardrobeItem(SqlAlchemyWardrobeRepository(session))
    item = await use_case.execute(
        item_id,
        current_user.id,
        title=payload.title,
        category=payload.category,
        color_main=payload.color_main,
        status=payload.status,
    )
    return ResponseModel(data=item_response(item))


@router.delete("/{item_id}", response_model=ResponseModel[DeleteWardrobeItemResponse])
async def delete_wardrobe_item(
    item_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[DeleteWardrobeItemResponse]:
    use_case = DeleteWardrobeItem(SqlAlchemyWardrobeRepository(session))
    deleted = await use_case.execute(item_id, current_user.id)
    return ResponseModel(data=DeleteWardrobeItemResponse(deleted=deleted))
