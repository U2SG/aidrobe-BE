"""Locating an item again inside the outfit photo it was imported from."""

from __future__ import annotations

import io
import uuid

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.vision import VisionResult
from app.application.wardrobe import item_photo
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


async def test_outfit_photo_is_sent_to_vision_inline_not_by_storage_url(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = uuid.uuid4()
    original = f"http://localhost:9000/aidrobe/users/{user_id}/wardrobe/outfit.jpg"
    with io.BytesIO() as buffer:
        Image.new("RGB", (60, 100), "white").save(buffer, "JPEG")
        photo = buffer.getvalue()

    async def load(url: str | None, key: str | None, *, user_id: uuid.UUID) -> bytes:
        assert url == original
        return photo

    monkeypatch.setattr(item_photo, "load_source_bytes", load)
    monkeypatch.setattr(item_photo, "resolve_import_source_key", lambda *_: f"users/{user_id}/wardrobe/outfit.jpg")

    db_session.add(User(id=user_id))
    await db_session.flush()
    db_session.add(ImportTask(
        user_id=user_id, input_type="outfit_image", source_type="album", input_payload={"image_url": original},
    ))
    item = WardrobeItem(
        user_id=user_id, title="米白色纽扣针织上衣", category="top", color_main="cream", original_image_url=original,
    )
    db_session.add(item)
    await db_session.flush()

    located = VisionResult(
        title="米白色纽扣针织上衣", category="top", color_main="cream", confidence_score=0.9, bbox=(0.1, 0.1, 0.9, 0.5),
    )
    sent: list[str] = []

    class Vision:
        async def analyze_outfit(self, image_url: str, locale: str) -> list[VisionResult]:
            sent.append(image_url)
            return [located]

        async def review_garment_crops(self, crops: list[bytes], garments: list[VisionResult]) -> list[bool]:
            return [True]

    reference = await item_photo.item_reference(db_session, item, Vision(), "zh-CN")  # type: ignore[arg-type]

    assert len(sent) == 1 and sent[0].startswith("data:image/jpeg;base64,")
    assert reference.url == original
    assert reference.crop and reference.cutout_source
    with Image.open(io.BytesIO(reference.crop)) as tight, Image.open(io.BytesIO(reference.cutout_source)) as wide:
        assert wide.width > tight.width and wide.height > tight.height
