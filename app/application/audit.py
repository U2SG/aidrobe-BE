"""Audit log helpers — Phase E2.

Use case layer talks to this module rather than touching `AuditLog` directly,
so the actor/action vocabulary stays controlled in one place. Adding a new
event type means adding a new helper here, which is intentional friction —
audit events are user-visible (DSAR responses) and should be reviewed.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.audit_log import AuditLog

logger = structlog.get_logger(__name__)

Actor = Literal["user", "system", "admin"]


async def record(
    session: AsyncSession,
    *,
    actor: Actor,
    action: str,
    user_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditLog:
    """Append an audit entry. Caller owns commit.

    `payload` should NOT contain raw PII — store enough to identify the
    operation (e.g. "outfit_id was wear-recorded") but not the personal
    content. PIPL DSAR responses surface this table directly to users."""
    entry = AuditLog(
        user_id=user_id,
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        payload=payload,
    )
    session.add(entry)
    await session.flush()
    logger.info(
        "audit.recorded",
        actor=actor,
        action=action,
        user_id=str(user_id) if user_id else None,
        target_type=target_type,
        target_id=str(target_id) if target_id else None,
    )
    return entry


# Action vocabulary — extend with care, these are user-visible labels.
ACTION_USER_DEACTIVATE = "user.deactivate"
ACTION_USER_PURGE = "user.purge"
ACTION_USER_PURGE_FAILED = "user.purge_failed"
ACTION_ADMIN_VIEW_DEAD_JOBS = "admin.view_dead_jobs"
# BE#83 — data-trust surface.
ACTION_MEMORY_DELETED = "memory.deleted"
ACTION_MEMORY_MARKED_WRONG = "memory.marked_wrong"
ACTION_MEMORY_CLEARED = "memory.cleared"
ACTION_DATA_CLEARED = "user.data_cleared"
