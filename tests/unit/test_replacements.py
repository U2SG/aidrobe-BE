"""Phase D2 — replacement-candidate cascade."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.outfits.obstacles import (
    attach_replacement_candidates,
    detect_obstacles,
    detect_weather_mismatch,
)
from app.application.outfits.replacements import find_replacements
from app.common.enums import Category, ItemStatus
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    return user_id


async def _add_item(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    category: str = Category.TOP.value,
    color: str = "white",
    status: str = ItemStatus.WEARABLE.value,
    title: str = "shirt",
) -> WardrobeItem:
    item = WardrobeItem(
        user_id=user_id,
        title=title,
        category=category,
        color_main=color,
        status=status,
    )
    session.add(item)
    await session.flush()
    return item


async def test_find_replacements_prefers_same_category_color(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(db_session)
    target = await _add_item(db_session, user_id, color="white", title="dirty white shirt")
    same_color = await _add_item(db_session, user_id, color="white", title="other white shirt")
    other_color = await _add_item(db_session, user_id, color="blue", title="blue shirt")

    candidates = await find_replacements(db_session, target, k=3)
    # Same-color tier wins; blue isn't returned even though it's same category.
    assert same_color.id in candidates
    assert other_color.id not in candidates


async def test_find_replacements_falls_back_to_category_only(
    db_session: AsyncSession,
) -> None:
    """Item has a unique color; tier-2 query returns nothing → cascade
    drops to tier-3 (any same-category wearable item)."""
    user_id = await _seed_user(db_session)
    target = await _add_item(db_session, user_id, color="puce", title="unique color")
    other = await _add_item(db_session, user_id, color="navy", title="any other top")
    candidates = await find_replacements(db_session, target, k=3)
    assert candidates == [other.id]


async def test_find_replacements_excludes_non_wearable(db_session: AsyncSession) -> None:
    """A dirty/lost candidate must NOT be suggested as a replacement —
    suggesting another laundry item to fix a laundry obstacle would be
    actively unhelpful."""
    user_id = await _seed_user(db_session)
    target = await _add_item(db_session, user_id, color="white", title="target")
    await _add_item(
        db_session, user_id, color="white", status="dirty", title="dirty same-color"
    )
    wearable = await _add_item(db_session, user_id, color="white", title="wearable same-color")
    candidates = await find_replacements(db_session, target, k=3)
    assert candidates == [wearable.id]


async def test_find_replacements_excludes_self_and_outfit_peers(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(db_session)
    target = await _add_item(db_session, user_id, title="target")
    peer = await _add_item(db_session, user_id, title="peer in same outfit")
    other = await _add_item(db_session, user_id, title="standalone")
    candidates = await find_replacements(db_session, target, exclude_ids=[peer.id], k=3)
    assert target.id not in candidates
    assert peer.id not in candidates
    assert other.id in candidates


async def test_find_replacements_returns_empty_when_nothing_matches(
    db_session: AsyncSession,
) -> None:
    """Single-item wardrobe → no replacement available. Must return []
    (not raise) so the caller can render the obstacle without a suggestion."""
    user_id = await _seed_user(db_session)
    target = await _add_item(db_session, user_id, title="only item")
    assert await find_replacements(db_session, target, k=3) == []


async def test_attach_replacement_candidates_skips_stale_and_weather(
    db_session: AsyncSession,
) -> None:
    """`stale` is informational and `weather_mismatch` targets the outfit;
    suggesting a replacement for either makes no UX sense."""
    user_id = await _seed_user(db_session)
    item = await _add_item(db_session, user_id, title="t1")
    other = await _add_item(db_session, user_id, title="t2")
    obstacles = [
        {"kind": "stale", "item_id": str(item.id), "severity": "info", "title": {}},
        {"kind": "weather_mismatch", "item_id": "", "severity": "warning", "title": {}},
        {"kind": "laundry", "item_id": str(item.id), "severity": "blocking", "title": {}},
    ]
    enriched = await attach_replacement_candidates(db_session, obstacles, [item])
    by_kind = {o["kind"]: o for o in enriched}
    assert "replacement_candidate_ids" not in by_kind["stale"]
    assert "replacement_candidate_ids" not in by_kind["weather_mismatch"]
    # Laundry obstacle DID get candidates attached.
    assert by_kind["laundry"]["replacement_candidate_ids"] == [str(other.id)]


def test_detect_weather_mismatch_flags_rain_without_outerwear(
    fake_today_context: None = None,
) -> None:
    """Rain probability above threshold + no outerwear = warning. We can't
    reuse db_session here; pure-data test."""
    from datetime import UTC, datetime

    user_id = uuid.uuid4()
    top = WardrobeItem(
        user_id=user_id, title="t", category="top", status="wearable", color_main="white"
    )
    bottom = WardrobeItem(
        user_id=user_id, title="b", category="bottom", status="wearable", color_main="black"
    )
    ctx = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "weather": {
            "temperature_c": 15.0,
            "feels_like_c": 14.0,
            "rain_probability": 0.8,
            "condition": "rain",
        },
    }
    out = detect_weather_mismatch([top, bottom], ctx)
    assert len(out) == 1
    assert out[0]["kind"] == "weather_mismatch"
    assert out[0]["severity"] == "warning"
    assert out[0]["item_id"] == ""


def test_detect_weather_mismatch_silent_with_outerwear() -> None:
    from datetime import UTC, datetime

    user_id = uuid.uuid4()
    top = WardrobeItem(
        user_id=user_id, title="t", category="top", status="wearable", color_main="white"
    )
    coat = WardrobeItem(
        user_id=user_id, title="c", category="outerwear", status="wearable", color_main="navy"
    )
    ctx = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "weather": {
            "temperature_c": 15.0,
            "feels_like_c": 14.0,
            "rain_probability": 0.9,
            "condition": "rain",
        },
    }
    assert detect_weather_mismatch([top, coat], ctx) == []


def test_detect_weather_mismatch_skips_stale_context() -> None:
    """A 1-day-old snapshot is too stale to act on. Skip rather than
    annoy the user with weather guidance from yesterday."""
    from datetime import UTC, datetime, timedelta

    user_id = uuid.uuid4()
    top = WardrobeItem(
        user_id=user_id, title="t", category="top", status="wearable", color_main="white"
    )
    ctx = {
        "fetched_at": (datetime.now(UTC) - timedelta(hours=24)).isoformat(),
        "weather": {
            "temperature_c": 15.0,
            "feels_like_c": 14.0,
            "rain_probability": 0.9,
            "condition": "rain",
        },
    }
    assert detect_weather_mismatch([top], ctx) == []


def test_detect_weather_mismatch_handles_missing_context() -> None:
    assert detect_weather_mismatch([], None) == []
    assert detect_weather_mismatch([], {}) == []


# Keep import side effects minimal for the existing detect_obstacles too.
_ = detect_obstacles
