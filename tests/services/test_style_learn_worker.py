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
from app.infrastructure.vision.fake_vision import FakeVision
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
    # bypasses dependency_overrides (CLAUDE.md test note). VISION_PROVIDER must be
    # non-"fake" (the worker guards against persisting FakeVision output), so we
    # set "qwen" and inject FakeVision directly via the container factory.
    settings = Settings(APP_ENV="test", ENABLE_VISION_STYLE_LEARNING=enabled, VISION_PROVIDER="qwen")
    monkeypatch.setattr(style_learn_worker, "get_settings", lambda: settings)
    monkeypatch.setattr("app.bootstrap.container.vision_service", lambda: FakeVision())


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


async def test_flag_on_with_fake_provider_degrades_to_heuristic(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag on but VISION_PROVIDER still 'fake' → must NOT persist FakeVision's
    deterministic output; degrade to the heuristic (no style_tags written)."""
    settings = Settings(APP_ENV="test", ENABLE_VISION_STYLE_LEARNING=True, VISION_PROVIDER="fake")
    monkeypatch.setattr(style_learn_worker, "get_settings", lambda: settings)
    _user_id, profile_id = await _seed(factory, image_url="https://cdn.example.com/milan.jpg")

    await learn_style_source(None, str(profile_id))

    async with factory() as s:
        profile = await s.get(UserStyleProfile, profile_id)
        assert profile is not None
        assert profile.learn_status == "learned"
        assert profile.style_tags is None
        assert profile.learned_summary is not None
        assert profile.learned_summary["source"] == "heuristic"


async def test_vision_rate_limit_degrades_to_heuristic(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over the per-user vision budget → degrade to the heuristic, don't burn
    Qwen calls (a /style/sources/batch can't bypass VISION_MAX_CALLS_PER_USER_HOUR)."""
    from app.infrastructure.vision.rate_limit import InMemoryVisionRateLimiter

    _set_flag(monkeypatch, enabled=True)
    # max_calls=0 → check() always raises immediately.
    monkeypatch.setattr("app.bootstrap.container.vision_rate_limiter", lambda: InMemoryVisionRateLimiter(0))
    _user_id, profile_id = await _seed(factory, image_url="https://cdn.example.com/milan.jpg")

    await learn_style_source(None, str(profile_id))

    async with factory() as s:
        profile = await s.get(UserStyleProfile, profile_id)
        assert profile is not None
        assert profile.learn_status == "learned"
        assert profile.style_tags is None
        assert profile.learned_summary is not None
        assert profile.learned_summary["source"] == "heuristic"


async def test_learned_tags_drive_recommendation_template(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The payoff: learned style_tags actually select a template in the live
    recommendation style_context (previously the dict shape was ignored)."""
    from app.application.recommendations import style_context

    _set_flag(monkeypatch, enabled=True)
    # build_style_context reads get_settings on its OWN module (lru_cached).
    monkeypatch.setattr(
        style_context, "get_settings", lambda: Settings(APP_ENV="test", ENABLE_VISION_STYLE_LEARNING=True)
    )
    user_id, profile_id = await _seed(factory, image_url="https://cdn.example.com/milan.jpg")

    await learn_style_source(None, str(profile_id))

    async with factory() as s:
        ctx = await style_context.build_style_context(user_id, s)
    # minimal + clean + black + white overlap the minimal_commute rule set.
    assert ctx["template_id"] == "minimal_commute_v1"
    assert "minimal" in ctx["active_tags"]
    assert ctx["confidence"] > 0.0


async def test_vision_tags_gated_off_on_rollback_but_user_tags_kept(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clean rollback: with the flag OFF, previously vision-learned style_tags
    stop driving /today, but user-set style_tags (source != "vision") still do."""
    from app.application.recommendations import style_context

    # Learn one inspiration via vision (flag ON).
    _set_flag(monkeypatch, enabled=True)
    user_id, profile_id = await _seed(factory, image_url="https://cdn.example.com/milan.jpg")
    await learn_style_source(None, str(profile_id))

    # Add a user-set style_tag profile (NOT vision-derived).
    async with factory() as s:
        s.add(
            UserStyleProfile(
                user_id=user_id,
                source_type="style_tag",
                style_tags={"tags": ["floral", "pastel"]},
                weight=1.0,
                learn_status="learned",
            )
        )
        await s.commit()

    # Roll the feature back OFF.
    monkeypatch.setattr(
        style_context, "get_settings", lambda: Settings(APP_ENV="test", ENABLE_VISION_STYLE_LEARNING=False)
    )
    async with factory() as s:
        ctx = await style_context.build_style_context(user_id, s)
    # Vision tags (minimal/black/white) excluded → user floral/pastel win.
    assert ctx["template_id"] == "soft_feminine_v1"
    assert "minimal" not in ctx["active_tags"]
    assert "floral" in ctx["active_tags"]
