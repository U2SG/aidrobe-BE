"""Only current, model-compatible garment vectors may influence ranking."""

import os
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.outfits.replacements import find_replacement_candidates
from app.application.ports.vision import VisionResult
from app.application.purchase.compute_similarity_evidence import _rank_by_embedding
from app.application.purchase.wardrobe_similarity import OwnedItemFacts
from app.application.shopping.sense_check import SenseCheck
from app.infrastructure.persistence.models.embedding import EMBEDDING_DIM, ItemEmbedding
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.vision.fake_vision import FakeVision


async def test_current_model_and_source_are_shared_by_all_consumers(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    # Fixed ids make the purchase-evidence anchor deterministic.
    anchor, stale, wrong_model, current, historical = [
        WardrobeItem(
            id=uuid.UUID(int=i), user_id=user_id, category="top", color_main="white", title=f"Shirt {i}",
            embedding_source_version="v2" if i < 5 else None,
        )
        for i in range(1, 6)
    ]
    db_session.add_all([anchor, stale, wrong_model, current, historical])
    await db_session.flush()
    near = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
    far = [0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 2)
    db_session.add_all([
        ItemEmbedding(item_id=anchor.id, model_name="fake:512", source_version="v2", vector=near),
        ItemEmbedding(item_id=stale.id, model_name="fake:512", source_version="v1", vector=near),
        ItemEmbedding(item_id=wrong_model.id, model_name="other", source_version="v2", vector=near),
        ItemEmbedding(item_id=current.id, model_name="fake:512", source_version="v2", vector=far),
        ItemEmbedding(item_id=historical.id, model_name="fake:512", vector=near),
    ])
    await db_session.flush()
    replacements = await find_replacement_candidates(db_session, anchor)
    sense_check = SenseCheck(db_session, FakeVision())
    vision = VisionResult("Shirt", "top", "white", 1.0)
    neighbours = await sense_check._find_similar(user_id, vision, "en-US")

    if os.getenv("PGVECTOR_ENABLED", "false").lower() in ("1", "true", "yes"):
        assert [candidate.item.id for candidate in replacements] == [current.id]
        assert [neighbour.item_id for neighbour in neighbours] == [current.id]
        facts = [
            OwnedItemFacts(
                item_id=item.id,
                category=item.category,
                color_main=item.color_main,
                material=None,
                garment_type="shirt",
                style_tags=(),
                wear_scenes=(),
            )
            for item in (anchor, stale, wrong_model, current, historical)
        ]
        ranked = await _rank_by_embedding(db_session, user_id, "top", facts)
        assert [fact.item_id for fact in ranked] == [anchor.id, current.id, stale.id, wrong_model.id, historical.id]
        anchor.embedding_source_version = "v3"
        current.embedding_source_version = "v3"
        await db_session.flush()
        replacements = await find_replacement_candidates(db_session, anchor)
        neighbours = await sense_check._find_similar(user_id, vision, "en-US")
    assert {candidate.item.id for candidate in replacements} == {stale.id, wrong_model.id, current.id, historical.id}
    assert all(candidate.similarity == 0.9 for candidate in replacements)
    assert {neighbour.item_id for neighbour in neighbours} == {
        anchor.id, stale.id, wrong_model.id, current.id, historical.id,
    }
    assert all(neighbour.similarity == 0.9 for neighbour in neighbours)
