"""Explicit experience stages stay distinct, owned, recoverable and revocable."""

import asyncio
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.application.outfits import experiences as experiences_module
from app.application.outfits.experiences import (
    StylingExperienceInput,
    StylingExperienceService,
    recent_experience_context,
)
from app.application.outfits.wear import WearOutfit
from app.application.ports.llm import ToolAskAnswer, ToolExecutor, ToolSpec
from app.application.users.clear_data import clear_user_data
from app.common.exceptions import FriendlyException
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.ask_conversation import (
    AskConversation,
    AskConversationState,
    AskStylingRevision,
)
from app.infrastructure.persistence.models.preference_memory import UserPreferenceMemory
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitFeedback,
    OutfitItem,
    RecommendationEvent,
    WearHistory,
)
from app.infrastructure.persistence.models.styling_experience import StylingExperience
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.preference_memory_repo import SqlAlchemyPreferenceMemoryRepository
from app.infrastructure.persistence.repositories.recommendation_repo import SqlAlchemyRecommendationRepository
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import styling_experiences as experience_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers
from tests.api.test_designer_sessions import _setup as setup_designer


async def _seed(session: AsyncSession) -> tuple[User, Outfit, AskConversation]:
    user = User(id=uuid.uuid4())
    session.add(user)
    await session.flush()
    session.add(UserSettings(user_id=user.id, timezone="UTC"))
    daily = DailyRecommendation(
        user_id=user.id, recommendation_date=date(2026, 1, 1), generation_source="rules",
    )
    item = WardrobeItem(
        user_id=user.id, title="Private garment name", category="top", color_main="blue",
    )
    conversation = AskConversation(user_id=user.id, styling_revision=1)
    session.add_all([daily, item, conversation])
    await session.flush()
    outfit = Outfit(
        user_id=user.id, daily_recommendation_id=daily.id, outfit_type="safe",
        title="Private outfit name", reason_text="test", score_total=Decimal("1"),
        rank_order=1, created_at=datetime.now(UTC),
    )
    session.add(outfit)
    await session.flush()
    session.add_all([
        OutfitItem(
            outfit_id=outfit.id, item_id=item.id, role="top", display_order=1, created_at=datetime.now(UTC),
        ),
        AskStylingRevision(
            user_id=user.id, conversation_id=conversation.id, revision=1, outfit_id=outfit.id,
            request_id=uuid.uuid4(), request_fingerprint="a" * 64, constraints={}, status="draft",
        ),
        AskConversationState(
            conversation_id=conversation.id, user_id=user.id, summary="An old interpretation of an experience",
        ),
    ])
    await session.flush()
    return user, outfit, conversation


def _app(session: AsyncSession, monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True) -> FastAPI:
    settings = Settings(
        APP_ENV="dev", ENABLE_DESIGNER_SESSIONS=enabled, ENABLE_FEEDBACK_LEARNING=True,
        ENABLE_PREFERENCE_MEMORY=True,
    )
    monkeypatch.setattr(experience_router, "get_settings", lambda: settings)
    monkeypatch.setattr(experiences_module, "get_settings", lambda: settings)
    app = FastAPI()
    setup_exception_handlers(app)
    app.dependency_overrides[get_db_session] = lambda: session
    app.dependency_overrides[get_settings] = lambda: settings
    app.include_router(experience_router.router)
    return app


def _body(outfit: Outfit, conversation: AskConversation, **changes: object) -> dict[str, object]:
    return {
        "request_id": str(uuid.uuid4()), "outfit_id": str(outfit.id), "kind": "appreciated",
        "conversation_id": str(conversation.id), "revision": 1, **changes,
    }


@pytest.mark.parametrize("kind", ["appreciated", "want_to_try"])
async def test_appreciation_and_intent_never_record_wear_or_learn_preferences(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    user, outfit, conversation = await _seed(db_session)
    app = _app(db_session, monkeypatch)
    payload = _body(outfit, conversation, kind=kind, occasion="casual", note="Just the colours")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/v1/styling-experiences", headers={"X-User-Id": str(user.id)}, json=payload)
        replay = await client.post("/v1/styling-experiences", headers={"X-User-Id": str(user.id)}, json=payload)
        listed = await client.get(
            "/v1/styling-experiences", headers={"X-User-Id": str(user.id)}, params={"outfit_id": str(outfit.id)},
        )
    assert first.status_code == replay.status_code == listed.status_code == 200
    assert replay.json() == first.json()
    assert listed.json() == {"items": [first.json()], "latest_worn_date": None}
    for model in (OutfitFeedback, WearHistory, RecommendationEvent, UserPreferenceMemory):
        assert await db_session.scalar(select(func.count()).select_from(model)) == 0
    assert await db_session.scalar(select(func.count()).select_from(StylingExperience)) == 1
    assert await SqlAlchemyRecommendationRepository(db_session).daily_has_engagement(outfit.daily_recommendation_id)


async def test_wear_feedback_needs_the_exact_owned_wear_fact(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, outfit, conversation = await _seed(db_session)
    app = _app(db_session, monkeypatch)
    worn_date = date(2026, 1, 1)
    payload = _body(outfit, conversation, kind="wear_feedback", worn_date=worn_date.isoformat(), feedback="like")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"X-User-Id": str(user.id)}
        missing = await client.post("/v1/styling-experiences", headers=headers, json=payload)
        assert missing.status_code == 409
        await WearOutfit(SqlAlchemyRecommendationRepository(db_session)).execute(outfit.id, user.id, worn_date)
        wrong_day = await client.post(
            "/v1/styling-experiences", headers=headers,
            json={**payload, "worn_date": "2026-01-02"},
        )
        assert wrong_day.status_code == 409
        saved = await client.post("/v1/styling-experiences", headers=headers, json=payload)
        listing = await client.get("/v1/styling-experiences", headers=headers, params={"outfit_id": str(outfit.id)})
    assert saved.status_code == 200
    assert saved.json()["kind"] == "wear_feedback" and saved.json()["feedback"] == "like"
    assert listing.json()["latest_worn_date"] == worn_date.isoformat()
    assert await db_session.scalar(select(func.count()).select_from(WearHistory)) == 1
    assert await db_session.scalar(select(func.count()).select_from(OutfitFeedback)) == 0
    assert await db_session.scalar(select(func.count()).select_from(UserPreferenceMemory)) == 0
    context = await recent_experience_context(db_session, user.id)
    assert context is not None and '"kind":"wear_feedback"' in context and '"feedback":"like"' in context


@pytest.mark.parametrize("changes", [
    {"kind": "appreciated", "feedback": "like"},
    {"kind": "want_to_try", "worn_date": "2026-01-01"},
    {"kind": "wear_feedback", "feedback": "like"},
    {"kind": "wear_feedback", "worn_date": "2026-01-01"},
    {"revision": None},
    {"conversation_id": None},
    {"note": "x" * 501},
])
async def test_invalid_experience_combinations_are_rejected_before_writing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, changes: dict[str, object],
) -> None:
    user, outfit, conversation = await _seed(db_session)
    app = _app(db_session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/styling-experiences", headers={"X-User-Id": str(user.id)},
            json=_body(outfit, conversation, **changes),
        )
    assert response.status_code == 422
    assert await db_session.scalar(select(func.count()).select_from(StylingExperience)) == 0


async def test_experiences_are_scoped_to_owner_and_exact_design_version(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, outfit, conversation = await _seed(db_session)
    other, other_outfit, other_conversation = await _seed(db_session)
    app = _app(db_session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"X-User-Id": str(user.id)}
        foreign = await client.post(
            "/v1/styling-experiences", headers=headers, json=_body(other_outfit, other_conversation),
        )
        wrong_version = await client.post(
            "/v1/styling-experiences", headers=headers, json=_body(outfit, conversation, revision=2),
        )
        wrong_conversation = await client.post(
            "/v1/styling-experiences", headers=headers,
            json=_body(outfit, conversation, conversation_id=str(other_conversation.id)),
        )
        saved = await client.post("/v1/styling-experiences", headers=headers, json=_body(outfit, conversation))
        other_headers = {"X-User-Id": str(other.id)}
        foreign_get = await client.get(
            "/v1/styling-experiences", headers=other_headers, params={"outfit_id": str(outfit.id)},
        )
        foreign_delete = await client.delete(
            f"/v1/styling-experiences/{saved.json()['experience_id']}", headers=other_headers,
        )
        other_list = await client.get("/v1/styling-experiences", headers=other_headers)
    assert [
        r.status_code for r in (foreign, wrong_version, wrong_conversation, foreign_get, foreign_delete)
    ] == [404] * 5
    assert other_list.json()["items"] == []


async def test_atomic_correction_fences_stale_retries_and_old_context(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, outfit, conversation = await _seed(db_session)
    app = _app(db_session, monkeypatch)
    original = _body(outfit, conversation, note="I like everything")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"X-User-Id": str(user.id)}
        first = await client.post("/v1/styling-experiences", headers=headers, json=original)
        old_id = first.json()["experience_id"]
        correction = _body(outfit, conversation, note="Only the colours", supersedes_id=old_id)
        replaced = await client.post("/v1/styling-experiences", headers=headers, json=correction)
        replay = await client.post("/v1/styling-experiences", headers=headers, json=correction)
        stale = await client.post("/v1/styling-experiences", headers=headers, json=original)
        competing = await client.post(
            "/v1/styling-experiences", headers=headers,
            json=_body(outfit, conversation, note="A stale correction", supersedes_id=old_id),
        )
        listed = await client.get("/v1/styling-experiences", headers=headers)
    assert replaced.status_code == replay.status_code == 200
    assert replaced.json() == replay.json()
    assert stale.status_code == competing.status_code == 409
    assert listed.json()["items"] == [replaced.json()]
    state = await db_session.get(AskConversationState, conversation.id)
    assert state is not None and state.summary is None
    context = await recent_experience_context(db_session, user.id)
    assert context is not None and "Only the colours" in context and "I like everything" not in context


async def test_correction_cannot_relabel_another_kind_and_keeps_original_on_failure(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, outfit, conversation = await _seed(db_session)
    app = _app(db_session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"X-User-Id": str(user.id)}
        original = await client.post("/v1/styling-experiences", headers=headers, json=_body(outfit, conversation))
        changed_kind = await client.post(
            "/v1/styling-experiences", headers=headers,
            json=_body(outfit, conversation, kind="want_to_try", supersedes_id=original.json()["experience_id"]),
        )
        listed = await client.get("/v1/styling-experiences", headers=headers)
    assert changed_kind.status_code == 409
    assert listed.json()["items"] == [original.json()]


async def test_withdrawal_is_idempotent_and_does_not_remove_other_preferences(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, outfit, conversation = await _seed(db_session)
    preference = UserPreferenceMemory(
        user_id=user.id, preference_type="color", preference_value="blue", polarity="positive",
        confidence=Decimal("1"), source="explicit_user_statement", scope={}, evidence_at=datetime.now(UTC),
    )
    db_session.add(preference)
    await db_session.flush()
    app = _app(db_session, monkeypatch)
    payload = _body(outfit, conversation, note="Something I asked you to forget")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"X-User-Id": str(user.id)}
        saved = await client.post("/v1/styling-experiences", headers=headers, json=payload)
        path = f"/v1/styling-experiences/{saved.json()['experience_id']}"
        deleted = await client.delete(path, headers=headers)
        deleted_again = await client.delete(path, headers=headers)
        replay = await client.post("/v1/styling-experiences", headers=headers, json=payload)
        listed = await client.get("/v1/styling-experiences", headers=headers)
    assert deleted.status_code == deleted_again.status_code == 200
    assert deleted.json() == {"success": True}
    assert replay.status_code == 409 and listed.json()["items"] == []
    assert await recent_experience_context(db_session, user.id) is None
    assert preference.deleted_at is None and user.memory_cleared_at is None
    assert user.memory_context_reset_at is not None
    assert await db_session.scalar(select(func.count()).select_from(StylingExperience)) == 1


async def test_memory_reset_excludes_old_experience_but_fresh_observations_work(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, outfit, conversation = await _seed(db_session)
    app = _app(db_session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"X-User-Id": str(user.id)}
        await client.post(
            "/v1/styling-experiences", headers=headers, json=_body(outfit, conversation, note="Old observation"),
        )
        assert "Old observation" in (await recent_experience_context(db_session, user.id) or "")
        await SqlAlchemyPreferenceMemoryRepository(db_session).soft_delete_all_live(user.id)
        assert await recent_experience_context(db_session, user.id) is None
        await client.post(
            "/v1/styling-experiences", headers=headers,
            json=_body(outfit, conversation, kind="want_to_try", note="Fresh observation"),
        )
    context = await recent_experience_context(db_session, user.id)
    assert context is not None and "Fresh observation" in context and "Old observation" not in context
    assert str(user.id) not in context and str(outfit.id) not in context and str(conversation.id) not in context
    assert "Private outfit name" not in context and "Private garment name" not in context


async def test_request_reuse_rejects_different_payload_without_overwriting(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, outfit, conversation = await _seed(db_session)
    app = _app(db_session, monkeypatch)
    payload = _body(outfit, conversation, note="Original")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"X-User-Id": str(user.id)}
        first = await client.post("/v1/styling-experiences", headers=headers, json=payload)
        collision = await client.post("/v1/styling-experiences", headers=headers, json={**payload, "note": "Changed"})
        listed = await client.get("/v1/styling-experiences", headers=headers)
    assert collision.status_code == 409
    assert listed.json()["items"] == [first.json()]


async def test_another_device_cannot_create_duplicate_active_intent(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, outfit, conversation = await _seed(db_session)
    app = _app(db_session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"X-User-Id": str(user.id)}
        first = await client.post("/v1/styling-experiences", headers=headers, json=_body(outfit, conversation))
        duplicate = await client.post("/v1/styling-experiences", headers=headers, json=_body(outfit, conversation))
        assert duplicate.status_code == 409
        await client.delete(f"/v1/styling-experiences/{first.json()['experience_id']}", headers=headers)
        listed = await client.get("/v1/styling-experiences", headers=headers)
    assert listed.json()["items"] == []


async def test_parallel_devices_only_create_one_live_intent(db_session: AsyncSession) -> None:
    user, outfit, _ = await _seed(db_session)
    user_id, outfit_id = user.id, outfit.id
    await db_session.commit()
    assert isinstance(db_session.bind, AsyncConnection)
    await db_session.bind.commit()
    sessions = async_sessionmaker(db_session.bind.engine, expire_on_commit=False)

    async def record() -> int:
        async with sessions() as session:
            try:
                await StylingExperienceService(session, user_id).record(StylingExperienceInput(
                    request_id=uuid.uuid4(), outfit_id=outfit_id, kind="want_to_try",
                ))
                await session.commit()
                return 200
            except FriendlyException as exc:
                await session.rollback()
                return exc.status_code

    assert sorted(await asyncio.gather(record(), record())) == [200, 409]
    assert await db_session.scalar(select(func.count()).select_from(StylingExperience)) == 1


async def test_outfit_history_keeps_active_intents_when_wear_feedback_fills_the_page(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, outfit, conversation = await _seed(db_session)
    app = _app(db_session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"X-User-Id": str(user.id)}
        for kind in ("appreciated", "want_to_try"):
            saved = await client.post(
                "/v1/styling-experiences", headers=headers, json=_body(outfit, conversation, kind=kind),
            )
            assert saved.status_code == 200
        wear = WearOutfit(SqlAlchemyRecommendationRepository(db_session))
        for day in range(25):
            worn_date = date(2026, 1, 1) + timedelta(days=day)
            await wear.execute(outfit.id, user.id, worn_date)
            saved = await client.post(
                "/v1/styling-experiences", headers=headers,
                json=_body(
                    outfit, conversation, kind="wear_feedback", feedback="like", worn_date=worn_date.isoformat(),
                ),
            )
            assert saved.status_code == 200
        listing = await client.get(
            "/v1/styling-experiences", headers=headers, params={"outfit_id": str(outfit.id), "limit": 3},
        )
    records = listing.json()["items"]
    assert len(records) == 3
    assert {entry["kind"] for entry in records} == {"appreciated", "want_to_try", "wear_feedback"}
    assert next(entry for entry in records if entry["kind"] == "wear_feedback")["worn_date"] == "2026-01-25"


async def test_feature_gate_hides_experiences_without_deleting_them(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, outfit, conversation = await _seed(db_session)
    on = _app(db_session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=on), base_url="http://test") as client:
        saved = await client.post(
            "/v1/styling-experiences", headers={"X-User-Id": str(user.id)}, json=_body(outfit, conversation),
        )
    off = _app(db_session, monkeypatch, enabled=False)
    async with AsyncClient(transport=ASGITransport(app=off), base_url="http://test") as client:
        headers = {"X-User-Id": str(user.id)}
        listed = await client.get("/v1/styling-experiences", headers=headers)
        deleted = await client.delete(f"/v1/styling-experiences/{saved.json()['experience_id']}", headers=headers)
        posted = await client.post("/v1/styling-experiences", headers=headers, json=_body(outfit, conversation))
    assert listed.status_code == deleted.status_code == posted.status_code == 503
    assert await recent_experience_context(db_session, user.id) is None
    assert await db_session.scalar(select(func.count()).select_from(StylingExperience)) == 1


async def test_prompt_context_is_bounded_and_does_not_claim_visual_appreciation_was_wear(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, outfit, conversation = await _seed(db_session)
    app = _app(db_session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(10):
            next_outfit = Outfit(
                user_id=user.id, outfit_type="safe", title="Another look", reason_text="test",
                score_total=Decimal("1"), rank_order=1, created_at=datetime.now(UTC),
            )
            db_session.add(next_outfit)
            await db_session.flush()
            saved = await client.post(
                "/v1/styling-experiences", headers={"X-User-Id": str(user.id)},
                json=_body(next_outfit, conversation, conversation_id=None, revision=None, note="a" * 500),
            )
            assert saved.status_code == 200
    context = await recent_experience_context(db_session, user.id, limit=100)
    assert context is not None
    entries = json.loads(context.split("\n", 1)[1])
    assert len(entries) == 8
    assert all(entry["kind"] == "appreciated" and "worn_date" not in entry for entry in entries)
    assert all(len(entry["note"]) == 240 for entry in entries)


async def test_future_wear_fact_cannot_authorize_after_wear_feedback(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, outfit, conversation = await _seed(db_session)
    tomorrow = datetime.now(UTC).date() + timedelta(days=1)
    await WearOutfit(SqlAlchemyRecommendationRepository(db_session)).execute(outfit.id, user.id, tomorrow)
    app = _app(db_session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/styling-experiences", headers={"X-User-Id": str(user.id)},
            json=_body(outfit, conversation, kind="wear_feedback", feedback="like", worn_date=tomorrow.isoformat()),
        )
        listed = await client.get(
            "/v1/styling-experiences", headers={"X-User-Id": str(user.id)}, params={"outfit_id": str(outfit.id)},
        )
    assert response.status_code == 422
    assert listed.json()["latest_worn_date"] is None
    assert await db_session.scalar(select(func.count()).select_from(StylingExperience)) == 0


@pytest.mark.parametrize("removal", ["outfit", "account_data"])
async def test_experience_records_follow_user_data_and_outfit_deletion(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, removal: str,
) -> None:
    user, outfit, conversation = await _seed(db_session)
    app = _app(db_session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        saved = await client.post(
            "/v1/styling-experiences", headers={"X-User-Id": str(user.id)}, json=_body(outfit, conversation),
        )
    assert saved.status_code == 200
    if removal == "outfit":
        await db_session.execute(delete(Outfit).where(Outfit.id == outfit.id))
    else:
        await clear_user_data(db_session, user.id)
    await db_session.flush()
    assert await db_session.scalar(select(func.count()).select_from(StylingExperience)) == 0
    assert await db_session.get(User, user.id) is not None


async def test_experience_withdrawn_while_model_is_running_cannot_reenter_memory_or_summary(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, _, model, settings = await setup_designer(db_session, monkeypatch)
    monkeypatch.setattr(experience_router, "get_settings", lambda: settings)
    monkeypatch.setattr(experiences_module, "get_settings", lambda: settings)
    app.include_router(experience_router.router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-User-Id": str(user_id)},
    ) as client:
        first = await client.post("/v1/ask", json={
            "question": "先帮我搭一套", "design_mode": True, "request_id": str(uuid.uuid4()),
        })
        assert first.status_code == 200, first.text
        styling = first.json()["styling"]
        conversation_id = uuid.UUID(styling["conversation_id"])
        saved = await client.post("/v1/styling-experiences", json={
            "request_id": str(uuid.uuid4()), "outfit_id": styling["outfit"]["outfit_id"],
            "conversation_id": str(conversation_id), "revision": styling["revision"],
            "kind": "appreciated", "note": "Old observation that must be withdrawn",
        })
        assert saved.status_code == 200, saved.text
        experience_id = uuid.UUID(saved.json()["experience_id"])
        assert isinstance(db_session.bind, AsyncConnection)
        sessions = async_sessionmaker(db_session.bind.engine, expire_on_commit=False)

        async def delayed_answer(
            system_prompt: str, question: str, tools: Sequence[ToolSpec], execute: ToolExecutor,
            *, json_mode: bool = False, max_rounds: int = 4,
        ) -> ToolAskAnswer:
            assert "Old observation that must be withdrawn" in system_prompt
            updated = await execute("update_design_brief", {
                "evidence_text": question, "goal": "Old observation derived goal",
                "notes": ["Old observation derived condition"],
            })
            assert updated.content["updated"] is True

            async def withdraw_in_another_transaction() -> None:
                async with sessions() as other_session:
                    await StylingExperienceService(other_session, user_id).withdraw(experience_id)
                    await other_session.commit()

            await asyncio.wait_for(withdraw_in_another_transaction(), timeout=5)
            return ToolAskAnswer(json.dumps({
                "answer": "Old observation must not survive in a summary", "reply_kind": "general",
                "referenced_items": [], "stated_preferences": [{
                    "type": "color", "value": "blue", "polarity": "positive", "evidence_text": "我喜欢蓝色",
                }],
            }, ensure_ascii=False), 4)

        monkeypatch.setattr(model, "ask_with_tools", delayed_answer)
        response = await client.post("/v1/ask", json={
            "question": "我喜欢蓝色，继续讨论刚才的尝试", "conversation_id": str(conversation_id),
            "design_mode": True, "expected_revision": styling["revision"], "request_id": str(uuid.uuid4()),
        })
        recovered = await client.get(f"/v1/ask/conversations/{conversation_id}")
    assert response.status_code == 409, response.text
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["design_brief"] == first.json()["design_brief"]
    assert recovered.json()["styling"]["outfit"]["outfit_id"] == styling["outfit"]["outfit_id"]
    assert await db_session.scalar(select(func.count()).select_from(UserPreferenceMemory)) == 0
    state = await db_session.scalar(select(AskConversationState).where(
        AskConversationState.conversation_id == conversation_id
    ).execution_options(populate_existing=True))
    assert state is not None and state.summary is None
    assert "Old observation" not in json.dumps(state.last_outfit or {})
    assert await recent_experience_context(db_session, user_id) is None
