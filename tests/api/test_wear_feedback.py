import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.outfits.wear_feedback import wear_feedback_details
from app.infrastructure.persistence.models.ask_conversation import AskConversation, AskStylingRevision
from app.infrastructure.persistence.models.preference_memory import UserPreferenceMemory
from app.infrastructure.persistence.models.recommendation import Outfit, RecommendationEvent, WearHistory
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.recommendation_repo import SqlAlchemyRecommendationRepository
from tests.api.test_outfit_actions import build_outfit_app


async def _draft(session: AsyncSession, user_id: uuid.UUID) -> Outfit:
    outfit = Outfit(
        user_id=user_id,
        outfit_type="stylish",
        title="Saved design",
        reason_text="",
        score_total=Decimal("0"),
        rank_order=0,
        created_at=datetime.now(UTC) - timedelta(days=5),
    )
    session.add(outfit)
    await session.flush()
    return outfit


def _path(outfit: Outfit, segment: str = "wear-feedback") -> str:
    return f"/v1/recommendations/outfits/{outfit.id}/{segment}"


async def test_wear_feedback_on_old_draft_is_editable_and_separate_from_its_rating(
    db_session: AsyncSession,
) -> None:
    user_id, other_id = uuid.uuid4(), uuid.uuid4()
    db_session.add_all([User(id=user_id), User(id=other_id)])
    await db_session.flush()
    outfit = await _draft(db_session, user_id)
    app = build_outfit_app(db_session)
    headers = {"X-User-Id": str(user_id)}
    other_headers = {"X-User-Id": str(other_id)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        assert (await client.get(_path(outfit), headers=headers)).json()["data"] == {"feedback": None}
        assert (await client.get(_path(outfit), headers=other_headers)).status_code == 404
        assert (await client.put(
            _path(outfit), headers=other_headers, json={"feedback_type": "like"},
        )).status_code == 404
        assert (await client.put(
            _path(outfit), headers=headers, json={"feedback_type": "dislike", "reason_tags": ["unknown"]},
        )).status_code == 422
        assert (await client.put(
            _path(outfit), headers=headers, json={"feedback_type": "like", "note": "x" * 301},
        )).status_code == 422

        first = {"feedback_type": "dislike", "reason_tags": ["too_warm"], "note": "室内穿起来闷"}
        saved = await client.put(_path(outfit), headers=headers, json=first)
        assert saved.status_code == 200
        assert saved.json()["data"] == {"feedback": first}
        assert not await db_session.scalar(select(func.count()).select_from(RecommendationEvent))
        assert not await db_session.scalar(select(func.count()).select_from(WearHistory))
        assert not await db_session.scalar(select(func.count()).select_from(UserPreferenceMemory))

        rated = await client.post(_path(outfit, "feedback"), headers=headers, json={"feedback_type": "neutral"})
        assert rated.status_code == 200
        corrected = {"feedback_type": "like", "reason_tags": [], "note": None}
        for _ in range(2):
            response = await client.put(_path(outfit), headers=headers, json={"feedback_type": "like"})
            assert response.status_code == 200
            assert response.json()["data"] == {"feedback": corrected}
        assert (await client.get(_path(outfit), headers=headers)).json()["data"] == {"feedback": corrected}

    repo = SqlAlchemyRecommendationRepository(db_session)
    ordinary_feedback = await repo.get_recent_feedback(user_id)
    assert [row.feedback_type for row in ordinary_feedback] == ["neutral"]
    assert await db_session.scalar(select(func.count()).select_from(RecommendationEvent)) == 1


async def test_design_wear_feedback_uses_one_current_record_per_outfit_and_stays_in_the_conversation(
    db_session: AsyncSession,
) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    old_outfit = await _draft(db_session, user_id)
    new_outfit = await _draft(db_session, user_id)
    other_outfit = await _draft(db_session, user_id)
    conversation = AskConversation(user_id=user_id)
    other_conversation = AskConversation(user_id=user_id)
    db_session.add_all([conversation, other_conversation])
    await db_session.flush()
    for thread, revision, outfit, status in [
        (conversation, 1, old_outfit, "draft"),
        (conversation, 2, old_outfit, "adopted"),
        (conversation, 3, new_outfit, "draft"),
        (other_conversation, 1, other_outfit, "draft"),
    ]:
        db_session.add(AskStylingRevision(
            conversation_id=thread.id,
            user_id=user_id,
            revision=revision,
            outfit_id=outfit.id,
            status=status,
            request_id=uuid.uuid4(),
            request_fingerprint="seed",
        ))
    await db_session.flush()
    app = build_outfit_app(db_session)
    headers = {"X-User-Id": str(user_id)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        for outfit, tag in [
            (old_outfit, "too_warm"),
            (new_outfit, "too_cold"),
            (old_outfit, "comfortable"),
            (other_outfit, "too_formal"),
        ]:
            response = await client.put(
                _path(outfit), headers=headers, json={"feedback_type": "like", "reason_tags": [tag]},
            )
            assert response.status_code == 200

    records = await SqlAlchemyRecommendationRepository(db_session).list_design_wear_feedback(
        user_id, conversation.id
    )
    assert [(revision, row.outfit_id, wear_feedback_details(row).reason_tags) for revision, row in records] == [
        (1, old_outfit.id, ["comfortable"]),
        (3, new_outfit.id, ["too_cold"]),
    ]
