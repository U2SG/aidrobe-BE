"""Design style changes affect real candidates without overriding exclusions."""

import uuid
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.application.ask.butler import AskButler
from app.application.ask.design_types import StylingConstraints
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


async def _setup(session: AsyncSession, target_style: str) -> AskButler:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    pieces = [
        WardrobeItem(user_id=user_id, title="Formal shirt", category="top", color_main="black",
                     style_tags={"tags": ["formal"]}, wear_count=100),
        WardrobeItem(user_id=user_id, title="Neutral shirt", category="top", color_main="white",
                     style_tags={"tags": ["casual", "clean", "soft"]}, wear_count=40),
        WardrobeItem(user_id=user_id, title="Bright shirt", category="top", color_main="yellow",
                     style_tags=["casual"], wear_count=0),
    ]
    session.add_all(pieces)
    await session.commit()
    assert isinstance(session.bind, AsyncConnection)
    await session.bind.commit()
    question = "想调整这次的风格"
    butler = AskButler(
        session, user_id, pieces, today=date(2026, 11, 17), location=None, weather=None, lang="zh",
        focused_item_id=None, excluded_ids=set(), context=None,
        design_constraints=StylingConstraints(), question=question,
    )
    updated = await butler.update_design_brief({"evidence_text": question, "target_style": target_style})
    assert updated.content["updated"]
    return butler


async def test_composite_style_tags_rank_before_wear_frequency(db_session: AsyncSession) -> None:
    butler = await _setup(db_session, "CASUAL， clean; soft")
    result = await butler.search_wardrobe({"category": "top", "limit": 1})
    assert result.content["items"][0]["position"] == 2


async def test_vivid_style_prefers_known_bright_colors_but_respects_negative_preferences(
    db_session: AsyncSession,
) -> None:
    butler = await _setup(db_session, "vivid, casual, clean, soft")
    result = await butler.search_wardrobe({"category": "top", "limit": 1})
    assert result.content["items"][0]["color_main"] == "yellow"

    butler.effective_preferences = {"negative": [{"type": "color", "value": "yellow"}]}
    result = await butler.search_wardrobe({"category": "top", "limit": 1})
    assert result.content["items"][0]["color_main"] == "white"


async def test_explicit_color_filter_and_exclusion_remain_authoritative(db_session: AsyncSession) -> None:
    butler = await _setup(db_session, "vivid, casual")
    result = await butler.search_wardrobe({"category": "top", "color": "white"})
    assert [item["color_main"] for item in result.content["items"]] == ["white"]

    butler.excluded_ids.add(butler.item_ids[2])
    result = await butler.search_wardrobe({"category": "top"})
    assert 3 not in [item["position"] for item in result.content["items"]]


async def test_multi_tag_search_accepts_both_saved_tag_shapes(db_session: AsyncSession) -> None:
    butler = await _setup(db_session, "casual, clean")
    result = await butler.search_wardrobe({"category": "top", "style": "casual，clean"})
    assert {item["position"] for item in result.content["items"]} == {2, 3}


@pytest.mark.parametrize("style", ["vivid", "鲜艳", "vivid, casual"])
async def test_explicit_vivid_filter_uses_color_evidence_without_requiring_a_literal_tag(
    db_session: AsyncSession, style: str,
) -> None:
    butler = await _setup(db_session, "casual")
    butler.design_constraints = None
    result = await butler.search_wardrobe({"category": "top", "style": style, "limit": 1})
    assert result.content["items"][0]["color_main"] == "yellow"


async def test_decorated_target_date_sets_the_design_date(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.commit()
    question = "周五面试穿什么"
    butler = AskButler(
        db_session, user_id, [], today=date(2026, 11, 19), location=None, weather=None, lang="zh",
        focused_item_id=None, excluded_ids=set(), context=None,
        design_constraints=StylingConstraints(), question=question,
    )
    updated = await butler.update_design_brief(
        {"evidence_text": question, "when": "周五（2026-11-20）", "scene": "formal"}
    )
    assert updated.content["updated"] is True
    assert butler.requested_date == date(2026, 11, 20)
