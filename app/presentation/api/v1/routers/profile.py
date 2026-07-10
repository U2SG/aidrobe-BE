"""Profile data-trust router (BE#83) — review / correct / clear learned memory,
and clear all app data while keeping the account.

Gated by `ENABLE_DATA_TRUST` (fails closed with 403) so the FE only shows these
controls when the behavior is real. Every destructive action is audited.

Privacy: user-facing labels never expose body-shape or body-correction framing —
items whose stored value trips the copy guard or a body-shape term are shown
with a functional fit label instead ("偏好宽松版型"-style), never skipped
silently (the user must be able to see AND delete everything learned).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import audit
from app.application.safety.copy_guard import find_forbidden_terms
from app.application.users.clear_data import clear_user_data
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.cache.client import (
    get_redis_client,
    invalidate_daily_recommendation_cache,
)
from app.infrastructure.persistence.models.preference_memory import UserPreferenceMemory
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.preference_memory_repo import (
    SqlAlchemyPreferenceMemoryRepository,
)
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.common import ResponseModel

router = APIRouter(prefix="/v1/profile", tags=["profile"])

# Body-shape labels must never reach the user (issue rule: functional wording
# like "偏好宽松版型", never "梨形 / 显瘦 / 遮肉"). Complements the shared
# FORBIDDEN_COPY_TERMS (which covers body-shaming framing) with shape taxonomy.
_BODY_SHAPE_TERMS = ("梨形", "苹果型", "沙漏", "h型", "倒三角", "pear", "hourglass", "apple-shaped")

_SOURCE_LABELS = {
    "explicit_user_statement": "explicit",
    "feedback_signal": "feedback",
    "inferred_from_history": "inferred",
}

# Functional replacement labels when a stored fit value is unsafe to display.
_SAFE_FIT_LABELS = {"positive": "偏好这类版型", "negative": "不偏好这类版型", "neutral": "版型偏好"}


class MemoryItemResponse(BaseModel):
    id: uuid.UUID
    type: str
    label: str
    polarity: str
    source: str
    confidence: str
    created_at: str


class MemoryListResponse(BaseModel):
    preferences: list[MemoryItemResponse]


class MemoryMutationResponse(BaseModel):
    deleted: int


class ClearDataResponse(BaseModel):
    cleared_rows: int


def _require_data_trust() -> None:
    if not get_settings().ENABLE_DATA_TRUST:
        raise FriendlyException(
            error_codes.FORBIDDEN,
            "data trust surface is not enabled",
            status_code=403,
        )


def _confidence_bucket(confidence: float) -> str:
    if confidence >= 0.7:
        return "high"
    if confidence >= 0.4:
        return "medium"
    return "low"


def _safe_label(row: UserPreferenceMemory) -> str:
    value = row.preference_value
    lowered = value.lower()
    if find_forbidden_terms(value) or any(term in lowered for term in _BODY_SHAPE_TERMS):
        return _SAFE_FIT_LABELS.get(row.polarity, _SAFE_FIT_LABELS["neutral"])
    return value


@router.get("/memory", response_model=ResponseModel[MemoryListResponse])
async def list_learned_memory(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[MemoryListResponse]:
    _require_data_trust()
    rows = await SqlAlchemyPreferenceMemoryRepository(session).list_live(current_user.id)
    items = [
        MemoryItemResponse(
            id=row.id,
            type=row.preference_type,
            label=_safe_label(row),
            polarity=row.polarity,
            source=_SOURCE_LABELS.get(row.source, "inferred"),
            confidence=_confidence_bucket(float(row.confidence)),
            created_at=row.created_at.isoformat(),
        )
        for row in rows
    ]
    return ResponseModel(data=MemoryListResponse(preferences=items))


async def _delete_one(
    session: AsyncSession, user_id: uuid.UUID, preference_id: uuid.UUID, action: str
) -> ResponseModel[MemoryMutationResponse]:
    deleted = await SqlAlchemyPreferenceMemoryRepository(session).soft_delete(
        user_id, preference_id
    )
    if not deleted:
        # Not yours / unknown → 404, never reveal existence.
        raise FriendlyException(
            error_codes.NOT_FOUND, "Memory item not found", status_code=404
        )
    await audit.record(
        session,
        actor="user",
        action=action,
        user_id=user_id,
        target_type="preference_memory",
        target_id=preference_id,
    )
    return ResponseModel(data=MemoryMutationResponse(deleted=1))


@router.delete("/memory/{preference_id}", response_model=ResponseModel[MemoryMutationResponse])
async def delete_memory_item(
    preference_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[MemoryMutationResponse]:
    _require_data_trust()
    return await _delete_one(
        session, current_user.id, preference_id, audit.ACTION_MEMORY_DELETED
    )


@router.post(
    "/memory/{preference_id}/mark-wrong",
    response_model=ResponseModel[MemoryMutationResponse],
)
async def mark_memory_item_wrong(
    preference_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[MemoryMutationResponse]:
    """Same removal as DELETE, but audited distinctly — "this is wrong" is a
    correction signal future learning can weigh, not just a removal."""
    _require_data_trust()
    return await _delete_one(
        session, current_user.id, preference_id, audit.ACTION_MEMORY_MARKED_WRONG
    )


@router.delete("/memory", response_model=ResponseModel[MemoryMutationResponse])
async def clear_learned_memory(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[MemoryMutationResponse]:
    _require_data_trust()
    count = await SqlAlchemyPreferenceMemoryRepository(session).soft_delete_all_live(
        current_user.id
    )
    await audit.record(
        session,
        actor="user",
        action=audit.ACTION_MEMORY_CLEARED,
        user_id=current_user.id,
        payload={"cleared": count},
    )
    return ResponseModel(data=MemoryMutationResponse(deleted=count))


@router.delete("/data", response_model=ResponseModel[ClearDataResponse])
async def clear_all_app_data(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
) -> ResponseModel[ClearDataResponse]:
    """BE#83 — clear ALL app data (wardrobe, history, events, learned memory,
    style data, settings) while preserving the account. Real deletion, audited;
    the audit trail itself survives (audit_log has no users FK by design)."""
    _require_data_trust()
    cleared = await clear_user_data(session, current_user.id)
    # The /today response is Redis-cached until UTC day end; without this the
    # next GET would replay the PRE-clear outfits after we told the user their
    # data was cleared.
    await invalidate_daily_recommendation_cache(redis_client, current_user.id)
    await audit.record(
        session,
        actor="user",
        action=audit.ACTION_DATA_CLEARED,
        user_id=current_user.id,
        payload={"cleared_rows": cleared},
    )
    return ResponseModel(data=ClearDataResponse(cleared_rows=cleared))
