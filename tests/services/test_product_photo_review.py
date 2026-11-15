import pytest

from app.config.settings import Settings
from app.infrastructure.vision.openai_image_cutout import OpenAIImageCutout
from app.infrastructure.vision.qwen_vision import QwenVision


@pytest.mark.parametrize("matches_visible_details", [True, False])
async def test_product_photo_may_complete_hidden_shoe_parts(
    monkeypatch: pytest.MonkeyPatch, matches_visible_details: bool
) -> None:
    generator = OpenAIImageCutout(Settings(OPENAI_API_KEY="test", QWEN_API_KEY="test"))

    async def generate(images: list[bytes], prompt: str) -> bytes:
        assert images == [b"partly covered sneakers"]
        assert "finishing only the hidden parts" in prompt
        return b"complete sneakers"

    async def review(self: QwenVision, source: str, prompt: str, candidate: str) -> str:
        assert "Allow plain, consistent completion" in prompt
        assert "changed visible details" in prompt
        assert "sole shape" in prompt
        return '{"accepted": true}' if matches_visible_details else '{"accepted": false}'

    monkeypatch.setattr(generator, "_edit", generate)
    monkeypatch.setattr(QwenVision, "_request_content", review)
    result = await generator.extract(b"partly covered sneakers", "shoes", title="White sneakers", color="white")
    assert result.outcome == ("succeeded" if matches_visible_details else "quality_rejected")
    assert result.image == (b"complete sneakers" if matches_visible_details else None)
