"""Persisted scope, correction and evidence fencing regressions."""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.preferences.memory import (
    SOURCE_EXPLICIT,
    SOURCE_FEEDBACK,
    PreferenceMemoryError,
    forget_preference,
    live_negative_values_by_type,
    read_preference_memory,
    record_preference,
)
from app.infrastructure.persistence.models.ask_conversation import (
    AskConversation,
    AskConversationState,
    AskMessage,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.ask_conversation_repo import SqlAlchemyAskConversationRepository
from app.infrastructure.persistence.repositories.preference_memory_repo import SqlAlchemyPreferenceMemoryRepository


async def _setup(session: AsyncSession) -> tuple[User, SqlAlchemyPreferenceMemoryRepository]:
    user = User(id=uuid.uuid4())
    session.add(user)
    await session.flush()
    return user, SqlAlchemyPreferenceMemoryRepository(session)


async def test_scoped_neutral_is_shared_by_prompt_and_uncapped_scoring(db_session: AsyncSession) -> None:
    user, repo = await _setup(db_session)
    global_row = await record_preference(
        repo, user_id=user.id, preference_type="color", preference_value="red",
        polarity="negative", confidence=0.9, source=SOURCE_EXPLICIT,
    )
    scoped = await record_preference(
        repo, user_id=user.id, preference_type="color", preference_value="red",
        polarity="neutral", confidence=0.9, source=SOURCE_EXPLICIT,
        scope={"day_type": "weekend"},
    )
    assert scoped.id != global_row.id
    weekend = date(2026, 11, 15)
    weekday = date(2026, 11, 16)
    assert (await read_preference_memory(repo, user.id, target_date=weekend))["negative"] == []
    assert await live_negative_values_by_type(repo, user.id, "color", target_date=weekend) == set()
    assert await live_negative_values_by_type(repo, user.id, "color", target_date=weekday) == {"red"}
    assert await live_negative_values_by_type(
        repo, user.id, "color", target_date=weekday, overrides=[{"type": "color", "value": "red"}]
    ) == set()
    assert (await read_preference_memory(repo, user.id, target_date=weekday))["negative"]


async def test_new_explicit_neutral_cannot_be_undone_by_old_turn_or_weak_signal(db_session: AsyncSession) -> None:
    user, repo = await _setup(db_session)
    old_turn = datetime.now(UTC) - timedelta(hours=1)
    row = await record_preference(
        repo, user_id=user.id, preference_type="color", preference_value="red",
        polarity="negative", confidence=0.9, source=SOURCE_EXPLICIT, evidence_at=old_turn,
    )
    neutral = await record_preference(
        repo, user_id=user.id, preference_type="color", preference_value="red",
        polarity="neutral", confidence=0.9, source=SOURCE_EXPLICIT,
    )
    assert neutral.id == row.id
    for source, evidence in ((SOURCE_EXPLICIT, old_turn), (SOURCE_FEEDBACK, datetime.now(UTC))):
        retained = await record_preference(
            repo, user_id=user.id, preference_type="color", preference_value="red",
            polarity="negative", confidence=0.9, source=source, evidence_at=evidence,
        )
        assert retained.polarity == "neutral"
    assert await live_negative_values_by_type(repo, user.id, "color") == set()


async def test_deleted_memory_rejects_old_evidence_but_allows_new_explicit_statement(db_session: AsyncSession) -> None:
    user, repo = await _setup(db_session)
    old = datetime.now(UTC) - timedelta(days=1)
    row = await record_preference(
        repo, user_id=user.id, preference_type="formality", preference_value="too_formal",
        polarity="negative", confidence=0.4, source=SOURCE_FEEDBACK, evidence_at=old,
    )
    assert await repo.soft_delete(user.id, row.id)
    for source in (SOURCE_FEEDBACK, SOURCE_EXPLICIT):
        with pytest.raises(PreferenceMemoryError, match="revoked"):
            await record_preference(
                repo, user_id=user.id, preference_type="formality", preference_value="too_formal",
                polarity="negative", confidence=0.4, source=source, evidence_at=old,
            )
    # A caller that cannot identify the observations cannot bypass the barrier.
    with pytest.raises(PreferenceMemoryError, match="revoked"):
        await record_preference(
            repo, user_id=user.id, preference_type="formality", preference_value="too_formal",
            polarity="negative", confidence=0.4, source=SOURCE_FEEDBACK,
        )
    fresh = await record_preference(
        repo, user_id=user.id, preference_type="formality", preference_value="too_formal",
        polarity="neutral", confidence=0.9, source=SOURCE_EXPLICIT,
    )
    assert fresh.id != row.id and fresh.deleted_at is None


async def test_clear_empty_memory_blocks_new_conclusions_from_pre_reset_events(db_session: AsyncSession) -> None:
    user, repo = await _setup(db_session)
    old = datetime.now(UTC) - timedelta(minutes=1)
    assert await repo.soft_delete_all_live(user.id) == 0
    assert user.memory_cleared_at is not None
    with pytest.raises(PreferenceMemoryError, match="revoked"):
        await record_preference(
            repo, user_id=user.id, preference_type="fit", preference_value="wrong_fit",
            polarity="negative", confidence=0.4, source=SOURCE_FEEDBACK, evidence_at=old,
        )
    assert await repo.list_live(user.id) == []


async def test_conversation_forget_is_scoped_and_cannot_erase_a_newer_statement(db_session: AsyncSession) -> None:
    user, repo = await _setup(db_session)
    old_turn = datetime.now(UTC) - timedelta(minutes=1)
    row = await record_preference(
        repo, user_id=user.id, preference_type="color", preference_value="red",
        polarity="positive", confidence=0.9, source=SOURCE_EXPLICIT,
    )
    assert await repo.soft_delete_matching(
        user.id, preference_type="color", preference_value="red", evidence_at=old_turn
    ) == 0
    assert row.deleted_at is None
    assert await repo.soft_delete_matching(
        user.id, preference_type="color", preference_value="red", scope={"day_type": "weekend"}
    ) == 0
    assert await repo.soft_delete_matching(
        user.id, preference_type="color", preference_value="red", evidence_at=datetime.now(UTC)
    ) == 1
    assert row.deleted_at is not None


async def test_forgetting_absent_scoped_memory_blocks_only_that_key(db_session: AsyncSession) -> None:
    user, repo = await _setup(db_session)
    other_user, _ = await _setup(db_session)
    old_turn = datetime.now(UTC) - timedelta(minutes=2)
    forgotten_at = datetime.now(UTC) - timedelta(minutes=1)
    assert await forget_preference(
        repo, user_id=user.id, preference_type=" COLOR ", preference_value="  Red\x00  ",
        scope={"scene": " COMMUTE "}, evidence_at=forgotten_at,
    ) == 0
    assert user.memory_context_reset_at is not None
    assert user.memory_cleared_at is None
    assert await repo.list_live(user.id) == []
    assert await repo.learning_cutoffs(user.id) == (None, {})  # scoped forget does not revoke global feedback
    for source in (SOURCE_EXPLICIT, SOURCE_FEEDBACK):
        with pytest.raises(PreferenceMemoryError, match="revoked"):
            await record_preference(
                repo, user_id=user.id, preference_type="color", preference_value="RED",
                polarity="negative", confidence=0.9, source=source,
                scope={"scene": "commute"}, evidence_at=old_turn,
            )
    for owner, value, scope in (
        (user.id, "red", {}),
        (user.id, "red", {"scene": "casual"}),
        (user.id, "blue", {"scene": "commute"}),
        (other_user.id, "red", {"scene": "commute"}),
    ):
        allowed = await record_preference(
            repo, user_id=owner, preference_type="color", preference_value=value,
            polarity="negative", confidence=0.9, source=SOURCE_EXPLICIT,
            scope=scope, evidence_at=old_turn,
        )
        assert allowed.deleted_at is None


@pytest.mark.parametrize("already_remembered", [False, True])
async def test_named_forget_orders_evidence_by_user_turn_not_completion(
    db_session: AsyncSession, already_remembered: bool,
) -> None:
    user, repo = await _setup(db_session)
    forgotten_at = datetime.now(UTC) - timedelta(minutes=2)
    if already_remembered:
        await record_preference(
            repo, user_id=user.id, preference_type="color", preference_value="red",
            polarity="negative", confidence=0.9, source=SOURCE_EXPLICIT,
            evidence_at=forgotten_at - timedelta(seconds=30),
        )
    assert await forget_preference(
        repo, user_id=user.id, preference_type="color", preference_value="red", evidence_at=forgotten_at,
    ) == int(already_remembered)
    # This statement began after the forget, although both model turns finish
    # later. Using the forget's completion time would incorrectly reject it.
    fresh = await record_preference(
        repo, user_id=user.id, preference_type="color", preference_value="red",
        polarity="positive", confidence=0.9, source=SOURCE_EXPLICIT,
        evidence_at=forgotten_at + timedelta(seconds=30),
    )
    for old_forget in (forgotten_at, forgotten_at - timedelta(minutes=1)):
        assert await forget_preference(
            repo, user_id=user.id, preference_type="color", preference_value="RED", evidence_at=old_forget,
        ) == 0
    assert fresh.deleted_at is None
    assert await repo.learning_cutoffs(user.id) == (None, {("color", "red"): forgotten_at})
    for source in (SOURCE_EXPLICIT, SOURCE_FEEDBACK):
        with pytest.raises(PreferenceMemoryError, match="revoked"):
            await record_preference(
                repo, user_id=user.id, preference_type="color", preference_value="red",
                polarity="negative", confidence=0.9, source=source, evidence_at=forgotten_at,
            )
    assert await repo.list_live(user.id) == [fresh]


async def test_forget_commits_before_an_older_turn_first_writes_memory(postgres_url: str) -> None:
    engine = create_async_engine(postgres_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = uuid.uuid4()
    turn_started = asyncio.Event()
    finish_turn = asyncio.Event()
    task: asyncio.Task[None] | None = None

    async def delayed_memory_write() -> None:
        evidence_at = datetime.now(UTC)
        turn_started.set()
        await finish_turn.wait()
        async with factory.begin() as session:
            with pytest.raises(PreferenceMemoryError, match="revoked"):
                await record_preference(
                    SqlAlchemyPreferenceMemoryRepository(session), user_id=user_id,
                    preference_type="color", preference_value="red", polarity="negative",
                    confidence=0.9, source=SOURCE_EXPLICIT, evidence_at=evidence_at,
                )

    try:
        async with factory.begin() as session:
            session.add(User(id=user_id))
        task = asyncio.create_task(delayed_memory_write())
        await asyncio.wait_for(turn_started.wait(), timeout=10)
        async with factory.begin() as session:
            repo = SqlAlchemyPreferenceMemoryRepository(session)
            assert await repo.list_live(user_id) == []
            assert await forget_preference(
                repo, user_id=user_id, preference_type="color", preference_value="red",
            ) == 0
        finish_turn.set()
        await asyncio.wait_for(task, timeout=10)
        async with factory.begin() as session:
            repo = SqlAlchemyPreferenceMemoryRepository(session)
            assert await repo.list_live(user_id) == []
            fresh = await record_preference(
                repo, user_id=user_id, preference_type="color", preference_value="red",
                polarity="positive", confidence=0.9, source=SOURCE_EXPLICIT,
            )
            assert await repo.list_live(user_id) == [fresh]
    finally:
        finish_turn.set()
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()


async def test_expired_weak_memory_does_not_reach_any_reader(db_session: AsyncSession) -> None:
    user, repo = await _setup(db_session)
    row = await record_preference(
        repo, user_id=user.id, preference_type="color", preference_value="red",
        polarity="negative", confidence=0.4, source=SOURCE_FEEDBACK,
        evidence_at=datetime.now(UTC) - timedelta(days=15),
    )
    assert row.expires_at is not None and row.expires_at < datetime.now(UTC)
    assert await repo.list_live(user.id) == []
    assert await live_negative_values_by_type(repo, user.id, "color") == set()
    assert (await read_preference_memory(repo, user.id))["negative"] == []


async def test_deletion_resets_derived_summary_but_preserves_visible_history_and_current_constraints(
    db_session: AsyncSession,
) -> None:
    user, repo = await _setup(db_session)
    row = await record_preference(
        repo, user_id=user.id, preference_type="color", preference_value="red",
        polarity="negative", confidence=0.9, source=SOURCE_EXPLICIT,
    )
    conversation = AskConversation(user_id=user.id, title="Style")
    db_session.add(conversation)
    await db_session.flush()
    state = AskConversationState(
        conversation_id=conversation.id, user_id=user.id,
        summary="The user dislikes red", active_constraints=["comfortable shoes"],
        last_outfit={"item_refs": ["existing-piece"]},
    )
    message = AskMessage(
        conversation_id=conversation.id, user_id=user.id, role="user", content="I dislike red",
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add_all([state, message])
    await db_session.flush()
    assert await repo.soft_delete(user.id, row.id)
    assert state.summary is None
    assert state.active_constraints == ["comfortable shoes"]
    assert state.last_outfit == {"item_refs": ["existing-piece"]}
    conversations = SqlAlchemyAskConversationRepository(db_session)
    assert await conversations.list_messages(user.id, conversation.id) == [message]
    assert await conversations.list_messages(
        user.id, conversation.id, after=user.memory_context_reset_at
    ) == []


async def test_parallel_new_conversations_can_write_memory_without_fk_lock_upgrade_deadlock(
    postgres_url: str,
) -> None:
    engine = create_async_engine(postgres_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = uuid.uuid4()
    ready = 0
    both_inserted = asyncio.Event()

    async def write_from_new_conversation(value: str) -> None:
        nonlocal ready
        async with factory.begin() as session:
            session.add(AskConversation(user_id=user_id, title=value))
            await session.flush()  # each transaction holds a User FK KEY SHARE lock
            ready += 1
            if ready == 2:
                both_inserted.set()
            await both_inserted.wait()
            await record_preference(
                SqlAlchemyPreferenceMemoryRepository(session), user_id=user_id,
                preference_type="color", preference_value=value, polarity="positive",
                confidence=0.9, source=SOURCE_EXPLICIT,
            )

    try:
        async with factory.begin() as session:
            session.add(User(id=user_id))
        await asyncio.wait_for(asyncio.gather(
            write_from_new_conversation("red"), write_from_new_conversation("navy")
        ), timeout=10)
        async with factory() as session:
            rows = await SqlAlchemyPreferenceMemoryRepository(session).list_live(user_id)
            assert {row.preference_value for row in rows} == {"red", "navy"}
    finally:
        await engine.dispose()
