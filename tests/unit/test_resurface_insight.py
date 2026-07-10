"""BE#80 — resurface insight selection (pure, fake repo/items)."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.application.recommendations.resurface_insight import build_resurface_insight

_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


def _item(season_tags: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), season_tags=season_tags)


class _FakeRepo:
    def __init__(self, forgotten: list, resurfaced: set | None = None) -> None:
        self._forgotten = forgotten
        self._resurfaced = resurfaced or set()

    async def list_forgotten_wearable_items(self, user_id, cutoff, limit: int = 20):
        return self._forgotten

    async def recently_resurfaced_item_ids(self, user_id, since):
        return self._resurfaced


async def _build(repo, current_season="summer"):
    return await build_resurface_insight(
        repo, uuid.uuid4(), current_season, now=_NOW, min_unworn_days=30
    )


async def test_no_candidate_returns_none() -> None:
    assert await _build(_FakeRepo([])) is None


async def test_candidate_returned_with_safe_copy() -> None:
    item = _item({"tags": ["summer"]})
    insight = await _build(_FakeRepo([item]))
    assert insight is not None
    assert insight.item_id == item.id
    assert insight.reason == "unworn_suitable_today"
    # non-judgmental — no guilt/shame wording
    assert "浪费" not in insight.text
    assert "没穿" in insight.text or "合适" in insight.text


async def test_cooldown_skips_recently_surfaced_item() -> None:
    first = _item({"tags": ["summer"]})
    second = _item({"tags": ["summer"]})
    insight = await _build(_FakeRepo([first, second], resurfaced={first.id}))
    assert insight is not None
    assert insight.item_id == second.id


async def test_season_unsuitable_item_skipped() -> None:
    winter_only = _item({"tags": ["winter"]})
    assert await _build(_FakeRepo([winter_only]), current_season="summer") is None


async def test_all_season_and_untagged_items_are_suitable() -> None:
    all_season = _item({"tags": ["all_season"]})
    assert (await _build(_FakeRepo([all_season]))).item_id == all_season.id
    untagged = _item(None)
    assert (await _build(_FakeRepo([untagged]))).item_id == untagged.id


async def test_no_current_season_accepts_any() -> None:
    item = _item({"tags": ["winter"]})
    assert (await _build(_FakeRepo([item]), current_season=None)).item_id == item.id
