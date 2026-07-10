"""BE#83 — clear ALL app data for a user while preserving the account.

Metadata-driven: every table with an FK to `users.id` is wiped for this user
(DB-level ON DELETE CASCADE then clears their grandchildren, the same mechanism
the hard-purge worker relies on). New tables are therefore cleared BY DEFAULT —
the privacy-safe default — unless explicitly listed in `_PRESERVE`.

Preserved:
- `users` itself (the whole point: account/auth survive).
- `audit_log` has NO users FK by design (a purged user's trail must outlive the
  row), so the FK scan naturally skips it.

`user_settings` is intentionally cleared too — its `preferences_json` carries
learned data (fit_profile) and commute_exposure; `get_or_create_settings`
lazily recreates defaults on the next request.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import structlog
from sqlalchemy import delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.base import Base

logger = structlog.get_logger(__name__)

_PRESERVE = frozenset({"users"})


async def clear_user_data(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Delete this user's rows from every users-FK'd table except `_PRESERVE`.
    Returns the number of directly deleted rows (cascade deletions not counted).
    Caller owns commit and the audit entry."""
    total = 0
    # Reversed FK order so children of users-FK'd tables that ALSO have their
    # own users FK are deleted before their parents.
    for table in reversed(Base.metadata.sorted_tables):
        if table.name in _PRESERVE:
            continue
        fk_columns = [
            column
            for column in table.columns
            if any(fk.column.table.name == "users" for fk in column.foreign_keys)
        ]
        for column in fk_columns:
            result = cast(
                CursorResult[Any], await session.execute(delete(table).where(column == user_id))
            )
            deleted = int(result.rowcount or 0)
            if deleted:
                logger.info(
                    "user_data_cleared.table",
                    user_id=str(user_id),
                    table=table.name,
                    rows=deleted,
                )
            total += deleted
    await session.flush()
    return total
