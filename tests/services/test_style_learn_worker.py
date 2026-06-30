"""Real visual style learning (style_learn_worker).

Cover:
- flag ON + an image → Qwen-VL (FakeVision in tests) writes the consumed
  style_tags + a rich learned_summary, and those tags drive the recommendation
  template matcher (build_style_context) — the wire actually reaches an output.
- flag OFF → today's behavior exactly: URL-domain heuristic, style_tags untouched.
- flag ON but vision errors → fail-soft to the heuristic; the profile still
  finishes "learned" (a user's inspiration never fails to learn).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import Settings
from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.models.user import User
from app.infrastructure.workers import style_learn_worker
from app.infrastructure.workers.style_learn_worker import learn_style_source


@pytest_asyncio.fixture
async def factory(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(postgres_url, future=True)
    sm: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(style_learn_worker, "AsyncSessionLocal", sm)
    try:
        yield sm
    finally:
        await engine.dispose()


def _set_flag(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    # Flip the flag on the CONSUMING module — get_settings is lru_cached and
    # bypasses dependency_overrides (CLAUDE.md test note).
    settings = Settings(APP_ENV="test", ENABLE_VISION_STYLE_LEARNING=enabled)
    monkeypatch.setattr(style_learn_worker, "get_settings", lambda: settings)


async def _seed(
    sm: async_sessionmaker[AsyncSession],
    *,
    image_url: str | None,
    source_url: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    async with sm() as s:
        s.add(User(id=user_id))
        await s.flush()
        profile = UserStyleProfile(
            user_id=user_id,
            source_type="inspiration_image",
            image_url=image_url,
            source_url=source_url,
            weight=1.0,
            learn_status="queued",
        )
        s.add(profile)
        await s.flush()
        profile_id = profile.id
        await s.commit()
    return user_id, profile_id


async def test_flag_on_vision_writes_style_tags_and_summary(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_flag(monkeypatch, enabled=True)
    _user_id, profile_id = await _seed(factory, image_url="https://cdn.example.com/milan.jpg")

    await learn_style_source(None, str(profile_id))

    async with factory() as s:
        profile = await s.get(UserStyleProfile, profile_id)
        assert profile is not None
        assert profile.learn_status == "learned"
        # FakeVision.analyze_style → minimal/clean/tailored + black/white.
        assert profile.style_tags == {"tags": ["minimal", "clean", "tailored", "black", "white"]}
        summary = profile.learned_summary
        assert summary is not None
        assert summary["source"] == "vision"
        assert summary["style_tags"] == ["minimal", "clean", "tailored"]
        assert summary["color_palette"] == ["black", "white"]
        assert summary["confidence"] == 0.85


async def test_flag_off_uses_heuristic_and_leaves_style_tags_untouched(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_flag(monkeypatch, enabled=False)
    # Flag off short-circuits before any image fetch — no network for the URL.
    _user_id, profile_id = await _seed(factory, image_url="https://cdn.example.com/pinterest-x.jpg")

    await learn_style_source(None, str(profile_id))

    async with factory() as s:
        profile = await s.get(UserStyleProfile, profile_id)
        assert profile is not None
        assert profile.learn_status == "learned"
        # style_tags (the consumed column) is NOT written by the heuristic path.
        assert profile.style_tags is None
        summary = profile.learned_summary
        assert summary is not None
        assert summary["source"] == "heuristic"
        # pinterest substring in the URL → the domain heuristic tags.
        assert summary["style_tags"] == ["aesthetic", "inspiration"]


async def test_flag_on_vision_failure_falls_back_to_heuristic(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_flag(monkeypatch, enabled=True)
    # FakeVision raises when "fail" is in the URL → fail-soft to the heuristic.
    _user_id, profile_id = await _seed(factory, image_url="https://cdn.example.com/fail.jpg")

    await learn_style_source(None, str(profile_id))

    async with factory() as s:
        profile = await s.get(UserStyleProfile, profile_id)
        assert profile is not None
        # Still learned (never "failed") — a user's inspiration always finishes.
        assert profile.learn_status == "learned"
        assert profile.style_tags is None
        assert profile.learned_summary is not None
        assert profile.learned_summary["source"] == "heuristic"


async def test_learned_tags_drive_recommendation_template(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The payoff: learned style_tags actually select a template in the live
    recommendation style_context (previously the dict shape was ignored)."""
    from app.application.recommendations.style_context import build_style_context

    _set_flag(monkeypatch, enabled=True)
    user_id, profile_id = await _seed(factory, image_url="https://cdn.example.com/milan.jpg")

    await learn_style_source(None, str(profile_id))

    async with factory() as s:
        ctx = await build_style_context(user_id, s)
    # minimal + clean + black + white overlap the minimal_commute rule set.
    assert ctx["template_id"] == "minimal_commute_v1"
    assert "minimal" in ctx["active_tags"]
    assert ctx["confidence"] > 0.0
