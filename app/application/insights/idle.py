"""Single definition of an "idle" wardrobe item, shared by every insight."""

from datetime import date

from sqlalchemy import ColumnElement, Date, cast, func

from app.infrastructure.persistence.models.wardrobe import WardrobeItem


def idle_since_expr() -> ColumnElement[date]:
    """Last worn date, or the date the item was added if it was never worn."""
    return func.coalesce(WardrobeItem.last_worn_at, cast(WardrobeItem.created_at, Date))


def idle_predicate(cutoff: date) -> ColumnElement[bool]:
    """An item is idle when it has not been worn (or added) since ``cutoff``."""
    return idle_since_expr() < cutoff
