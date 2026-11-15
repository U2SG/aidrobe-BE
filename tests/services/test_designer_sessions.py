"""Designer versions preserve item identity, ownership and concurrent intent."""

import asyncio
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.ask.design_types import PreferenceOverride, StylingConstraints
from app.application.ask.styling import StylingService, fingerprint
from app.application.outfits.wear import WearOutfit
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.ask_conversation import AskStylingRevision
from app.infrastructure.persistence.models.recommendation import DailyRecommendation, Outfit, RecommendationEvent
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import SqlAlchemyRecommendationRepository


async def _seed(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID, list[WardrobeItem]]:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    items = [WardrobeItem(user_id=user_id, title=category, category=category, color_main="black")
             for category in ("top", "bottom", "shoes", "shoes", "top")]
    session.add_all(items)
    await session.flush()
    service = StylingService(session, user_id)
    conversation = await service.conversations.create_conversation(user_id=user_id, title="An interview")
    return user_id, conversation.id, items


async def _draft(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID, items: list[WardrobeItem],
) -> AskStylingRevision:
    request_id = uuid.uuid4()
    return await StylingService(session, user_id).save_draft(
        conversation_id=conversation_id, expected_revision=0, item_ids=[item.id for item in items[:3]],
        constraints=StylingConstraints(goal="An interview", locked_item_ids=[items[0].id]),
        request_id=request_id, request_fingerprint=fingerprint("seed", request_id=request_id),
        title="Interview", reason="A complete outfit.",
    )


async def test_draft_adoption_and_swap_share_real_outfits_without_overwriting_history(db_session: AsyncSession) -> None:
    user_id, conversation_id, items = await _seed(db_session)
    service = StylingService(db_session, user_id)
    repository = SqlAlchemyRecommendationRepository(db_session)
    original = await _draft(db_session, user_id, conversation_id, items)
    assert await repository.get_daily(user_id, date.today()) is None
    original_outfit = await service.owned_outfit(original.outfit_id)
    assert original_outfit.daily_recommendation_id is None

    adopt_id = uuid.uuid4()
    adopted = await service.adopt(conversation_id=conversation_id, expected_revision=1,
                                  request_id=adopt_id, day=date.today())
    replay = await service.adopt(conversation_id=conversation_id, expected_revision=1,
                                 request_id=adopt_id, day=date.today())
    assert replay.id == adopted.id and adopted.outfit_id == original.outfit_id
    assert adopted.revision == 2 and original.status == "draft"
    today = await repository.get_daily(user_id, date.today())
    assert today is not None and [outfit.id for outfit in today.outfits] == [original.outfit_id]

    replacement = await service.revise_outfit(outfit_id=adopted.outfit_id, request_id=uuid.uuid4(),
                                             item_id=items[2].id, replacement_item_id=items[3].id)
    current_outfit = await service.owned_outfit(replacement.outfit_id)
    assert replacement.status == "adopted" and replacement.outfit_id != adopted.outfit_id
    assert {row.item_id for row in current_outfit.items} == {items[0].id, items[1].id, items[3].id}
    assert {row.item_id for row in original_outfit.items} == {item.id for item in items[:3]}
    latest = await repository.get_daily(user_id, date.today())
    assert latest is not None and latest.id == replacement.recommendation_id
    assert latest.revision == 2 and [outfit.id for outfit in latest.outfits] == [replacement.outfit_id]

    await repository.add_outfit_favorite(user_id, replacement.outfit_id)
    await WearOutfit(repository).execute(replacement.outfit_id, user_id, date.today())
    worn_event = await db_session.scalar(select(RecommendationEvent).where(RecommendationEvent.event_type == "worn"))
    assert worn_event is not None and worn_event.outfit_id == replacement.outfit_id
    assert worn_event.recommendation_id == replacement.recommendation_id
    assert await repository.list_favorited_outfit_ids(user_id, [replacement.outfit_id]) == {replacement.outfit_id}
    # A background recompute cannot cascade-delete an adopted design.
    kept, should_write = await repository.replace_daily(DailyRecommendation(
        user_id=user_id, recommendation_date=date.today(), status="ready", generation_source="nightly"
    ))
    assert kept.id == latest.id and should_write is False


async def test_revision_checks_owner_locks_wearability_staleness_and_request_identity(db_session: AsyncSession) -> None:
    user_id, conversation_id, items = await _seed(db_session)
    service = StylingService(db_session, user_id)
    original = await _draft(db_session, user_id, conversation_id, items)
    other_id = uuid.uuid4()
    db_session.add(User(id=other_id))
    await db_session.flush()
    foreign = WardrobeItem(user_id=other_id, category="shoes", title="Foreign", color_main="white")
    db_session.add(foreign)
    await db_session.flush()

    for replacement, expected_status in ((foreign.id, 404), (items[4].id, 422)):
        with pytest.raises(FriendlyException) as error:
            await service.revise(conversation_id=conversation_id, expected_revision=1, request_id=uuid.uuid4(),
                                 replace=(items[2].id, replacement), locked_item_ids=None, excluded_item_ids=None)
        assert error.value.status_code == expected_status
    with pytest.raises(FriendlyException, match="Unlock"):
        await service.revise(conversation_id=conversation_id, expected_revision=1, request_id=uuid.uuid4(),
                             replace=(items[0].id, items[4].id), locked_item_ids=None, excluded_item_ids=None)
    items[3].status = "dirty"
    await db_session.flush()
    with pytest.raises(FriendlyException, match="no longer wearable"):
        await service.revise(conversation_id=conversation_id, expected_revision=1, request_id=uuid.uuid4(),
                             replace=(items[2].id, items[3].id), locked_item_ids=None, excluded_item_ids=None)
    items[3].status = "wearable"
    await db_session.flush()
    request_id = uuid.uuid4()
    updated = await service.revise(conversation_id=conversation_id, expected_revision=1, request_id=request_id,
                                   replace=(items[2].id, items[3].id), locked_item_ids=None, excluded_item_ids=None)
    replay = await service.revise(conversation_id=conversation_id, expected_revision=1, request_id=request_id,
                                  replace=(items[2].id, items[3].id), locked_item_ids=None, excluded_item_ids=None)
    assert replay.id == updated.id
    assert updated.outfit_id != original.outfit_id and updated.status == "draft"
    assert await service.recommendations.get_daily(user_id, date.today()) is None
    with pytest.raises(FriendlyException, match="request_id"):
        await service.revise(conversation_id=conversation_id, expected_revision=1, request_id=request_id,
                             replace=(items[2].id, items[3].id), locked_item_ids=[], excluded_item_ids=None)
    with pytest.raises(FriendlyException) as stale:
        await service.revise(conversation_id=conversation_id, expected_revision=1, request_id=uuid.uuid4(),
                             replace=None, locked_item_ids=[], excluded_item_ids=None)
    assert stale.value.status_code == 409
    with pytest.raises(FriendlyException) as foreign_read:
        await StylingService(db_session, other_id).owned_outfit(updated.outfit_id)
    assert foreign_read.value.status_code == 404


async def test_concurrent_design_edits_have_one_winner(postgres_url: str) -> None:
    engine = create_async_engine(postgres_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory.begin() as session:
            user_id, conversation_id, items = await _seed(session)
            await _draft(session, user_id, conversation_id, items)
            shoe_id, replacement_id = items[2].id, items[3].id

        async def edit() -> int:
            try:
                async with factory.begin() as session:
                    result = await StylingService(session, user_id).revise(
                        conversation_id=conversation_id, expected_revision=1, request_id=uuid.uuid4(),
                        replace=(shoe_id, replacement_id), locked_item_ids=None, excluded_item_ids=None,
                    )
                    return result.revision
            except FriendlyException as exc:
                return exc.status_code

        assert sorted(await asyncio.gather(edit(), edit())) == [2, 409]
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(AskStylingRevision)) == 2
            assert await session.scalar(select(func.count()).select_from(Outfit)) == 2
    finally:
        await engine.dispose()


async def test_adopt_default_target_and_retry_do_not_move_at_midnight(db_session: AsyncSession) -> None:
    user_id, conversation_id, items = await _seed(db_session)
    service = StylingService(db_session, user_id)
    tomorrow = date.today() + timedelta(days=1)
    draft = await service.save_draft(
        conversation_id=conversation_id, expected_revision=0, item_ids=[item.id for item in items[:3]],
        constraints=StylingConstraints(target_date=tomorrow), request_id=uuid.uuid4(),
        request_fingerprint=fingerprint("tomorrow"), title="Tomorrow", reason="Ready for tomorrow.",
    )
    request_id = uuid.uuid4()
    adopted = await service.adopt(conversation_id=conversation_id, expected_revision=1, request_id=request_id,
                                  default_day=date.today())
    repeated = await service.adopt(conversation_id=conversation_id, expected_revision=1, request_id=request_id,
                                   default_day=tomorrow + timedelta(days=1))
    assert adopted.id == repeated.id and adopted.outfit_id == draft.outfit_id
    assert adopted.recommendation_date == tomorrow
    assert await service.recommendations.get_daily(user_id, date.today()) is None


@pytest.mark.parametrize("explicit_date", [False, True])
async def test_first_adoption_advances_expired_draft_but_replay_keeps_original_day(
    db_session: AsyncSession, explicit_date: bool,
) -> None:
    user_id, conversation_id, items = await _seed(db_session)
    service = StylingService(db_session, user_id)
    today = date(2030, 1, 3)
    yesterday = today - timedelta(days=1)
    draft = await service.save_draft(
        conversation_id=conversation_id, expected_revision=0, item_ids=[item.id for item in items[:3]],
        constraints=StylingConstraints(target_date=yesterday, exploration=True,
                                       preference_overrides=[PreferenceOverride(type="color", value="red")]),
        request_id=uuid.uuid4(), request_fingerprint=fingerprint("expired"), title="Yesterday", reason="A draft.",
    )
    request_id = uuid.uuid4()
    requested = yesterday if explicit_date else None
    adopted = await service.adopt(
        conversation_id=conversation_id, expected_revision=1, request_id=request_id, day=requested, default_day=today,
    )
    replay = await service.adopt(
        conversation_id=conversation_id, expected_revision=1, request_id=request_id,
        day=requested, default_day=today + timedelta(days=1),
    )
    assert replay.id == adopted.id and adopted.recommendation_date == today
    assert adopted.constraints["target_date"] == today.isoformat()
    assert adopted.constraints["preference_overrides"] == [] and adopted.constraints["exploration"] is False
    assert draft.constraints["target_date"] == yesterday.isoformat()
    assert await service.recommendations.get_daily(user_id, yesterday) is None
    assert (await service.recommendations.get_daily(user_id, today)).id == adopted.recommendation_id


async def test_pending_brief_versions_serialize_before_the_first_outfit(postgres_url: str) -> None:
    engine = create_async_engine(postgres_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory.begin() as session:
            user_id, conversation_id, _ = await _seed(session)

        async def change(goal: str) -> int:
            try:
                async with factory.begin() as session:
                    service = StylingService(session, user_id)
                    conversation = await service.lock_conversation(conversation_id, 0)
                    await service.save_brief(conversation, StylingConstraints(goal=goal))
                    return conversation.styling_revision
            except FriendlyException as exc:
                return exc.status_code

        assert sorted(await asyncio.gather(change("Formal"), change("Casual"))) == [1, 409]
        async with factory() as session:
            service = StylingService(session, user_id)
            conversation = await service.conversations.get_owned_conversation(user_id, conversation_id)
            assert conversation is not None
            brief = service.brief(conversation, await service.current(conversation_id))
            assert brief is not None and brief.needs_outfit_update and brief.revision == 1
            assert await session.scalar(select(func.count()).select_from(Outfit)) == 0
    finally:
        await engine.dispose()


async def test_today_swap_keeps_pending_redesign_in_its_original_conversation(db_session: AsyncSession) -> None:
    user_id, conversation_id, items = await _seed(db_session)
    service = StylingService(db_session, user_id)
    await _draft(db_session, user_id, conversation_id, items)
    adopted = await service.adopt(
        conversation_id=conversation_id, expected_revision=1, request_id=uuid.uuid4(), day=date.today(),
    )
    conversation = await service.lock_conversation(conversation_id, 2)
    pending = StylingConstraints(goal="Try streetwear", locked_item_ids=[items[0].id])
    await service.save_brief(conversation, pending)
    replacement = await service.revise_outfit(
        outfit_id=adopted.outfit_id, request_id=uuid.uuid4(), item_id=items[2].id, replacement_item_id=items[3].id,
    )
    assert replacement.status == "adopted" and replacement.conversation_id != conversation_id
    assert replacement.outfit_id != adopted.outfit_id
    brief = service.brief(conversation, await service.current(conversation_id))
    assert brief is not None and brief.needs_outfit_update and brief.constraints == pending
    assert brief.revision == 3
    today = await service.recommendations.get_daily(user_id, date.today())
    assert today is not None and today.id == replacement.recommendation_id


async def test_expired_draft_defaults_to_current_local_day(db_session: AsyncSession) -> None:
    user_id, conversation_id, items = await _seed(db_session)
    service = StylingService(db_session, user_id)
    today = date.today()
    yesterday = today - timedelta(days=1)
    draft = await service.save_draft(
        conversation_id=conversation_id, expected_revision=0,
        item_ids=[item.id for item in items[:3]],
        constraints=StylingConstraints(target_date=yesterday), request_id=uuid.uuid4(),
        request_fingerprint=fingerprint("expired-draft"), title="Yesterday", reason="Saved earlier.",
    )
    request_id = uuid.uuid4()
    adopted = await service.adopt(
        conversation_id=conversation_id, expected_revision=1,
        request_id=request_id, default_day=today,
    )
    assert adopted.outfit_id == draft.outfit_id
    assert adopted.recommendation_date == today
    assert await service.recommendations.get_daily(user_id, today) is not None
    assert await service.recommendations.get_daily(user_id, yesterday) is None

    # A later retry must replay the original adoption even after local midnight.
    replay = await service.adopt(
        conversation_id=conversation_id, expected_revision=1,
        request_id=request_id, default_day=today + timedelta(days=1),
    )
    assert replay.id == adopted.id


async def test_explicit_past_adoption_uses_current_local_day(db_session: AsyncSession) -> None:
    user_id, conversation_id, items = await _seed(db_session)
    service = StylingService(db_session, user_id)
    await _draft(db_session, user_id, conversation_id, items)
    today = date.today()
    adopted = await service.adopt(
        conversation_id=conversation_id, expected_revision=1, request_id=uuid.uuid4(),
        day=today - timedelta(days=1), default_day=today,
    )
    assert adopted.recommendation_date == today
    assert adopted.constraints["target_date"] == today.isoformat()
    assert await service.recommendations.get_daily(user_id, today - timedelta(days=1)) is None
