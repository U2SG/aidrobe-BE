"""Shopping Sense-Check router — M17. Off by default; V3-style direct return.

Pipeline: vision-identify the candidate → embedding-similarity match against
the user's wardrobe → assess duplication & gap → verdict. No LLM. History is
persisted regardless of the flag; the flag only gates exposure (503 when off).
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.vision import VisionPort
from app.application.shopping.sense_check import (
    SIM_CATEGORY_COLOR,
    ListSenseChecks,
    SenseCheck,
)
from app.bootstrap.container import vision_service
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.common.i18n import I18nMessage
from app.config.settings import get_settings
from app.infrastructure.persistence.models.shopping import ShoppingSenseCheck
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.render_url import resolve_render_url
from app.presentation.api.v1.schemas.shopping import (
    SenseCheckHistoryItem,
    SenseCheckHistoryResponse,
    SenseCheckRequest,
    SenseCheckResponse,
    SimilarItemResponse,
)

router = APIRouter(prefix="/v1/shopping", tags=["shopping"])


def _gate() -> None:
    if not get_settings().ENABLE_SENSE_CHECK:
        raise FriendlyException(
            error_codes.FORBIDDEN, "Shopping sense-check is not enabled", status_code=503
        )


def get_vision_service() -> VisionPort:
    return vision_service()


def _display_reason(payload: dict[str, object]) -> I18nMessage | None:
    raw = payload.get("display_reason")
    if not isinstance(raw, dict):
        return None
    try:
        return I18nMessage(**raw)
    except ValueError:
        return None


def _similar_item_count(payload: dict[str, object], raw_similar: object) -> int:
    count = payload.get("similar_item_count")
    if isinstance(count, int):
        return count
    if not isinstance(raw_similar, list):
        return 0
    return sum(
        1
        for item in raw_similar
        if isinstance(item, dict) and float(item.get("similarity", 0.0)) >= SIM_CATEGORY_COLOR
    )


def _history_item(record: ShoppingSenseCheck) -> SenseCheckHistoryItem:
    payload = record.payload or {}
    raw_similar = payload.get("similar_items", [])
    similar = [
        SimilarItemResponse(item_id=s["item_id"], similarity=s["similarity"])
        for s in raw_similar
        if isinstance(s, dict)
    ]
    return SenseCheckHistoryItem(
        sense_check_id=record.id,
        verdict=record.verdict,
        reason=record.reason,
        display_reason=_display_reason(payload),
        similar_item_count=_similar_item_count(payload, raw_similar),
        image_url=resolve_render_url(record.object_key, record.image_url),
        source_url=record.source_url,
        similar_items=similar,
        wardrobe_gap=payload.get("wardrobe_gap", []),
        outfit_potential=payload.get("outfit_potential", 0),
        created_at=record.created_at,
    )


@router.post("/sense-check", response_model=SenseCheckResponse)
async def sense_check(
    payload: SenseCheckRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: Annotated[User, Depends(get_current_user)],
    vision: Annotated[VisionPort, Depends(get_vision_service)],
) -> SenseCheckResponse:
    _gate()
    if payload.image_url is None and payload.source_url is None:
        raise FriendlyException(
            error_codes.VALIDATION_ERROR,
            "image_url or source_url is required",
            status_code=422,
        )
    result, _ = await SenseCheck(session, vision).execute(
        current_user.id,
        image_url=payload.image_url,
        source_url=payload.source_url,
    )
    return SenseCheckResponse(
        verdict=result.verdict,
        reason=result.reason,
        display_reason=result.display_reason,
        similar_item_count=result.similar_item_count,
        similar_items=[
            SimilarItemResponse(item_id=s.item_id, similarity=s.similarity)
            for s in result.similar_items
        ],
        wardrobe_gap=result.wardrobe_gap,
        outfit_potential=result.outfit_potential,
    )


@router.get("/history", response_model=SenseCheckHistoryResponse)
async def history(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: Annotated[User, Depends(get_current_user)],
    limit: int = 20,
    offset: int = 0,
) -> SenseCheckHistoryResponse:
    _gate()
    records = await ListSenseChecks(session).execute(
        current_user.id, limit=limit, offset=offset
    )
    return SenseCheckHistoryResponse(items=[_history_item(r) for r in records])
