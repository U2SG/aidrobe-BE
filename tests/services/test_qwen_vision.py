import pytest

from app.config.settings import Settings
from app.infrastructure.vision.qwen_vision import QwenVision


async def test_analyze_outfit_keeps_top_and_bottom(monkeypatch: pytest.MonkeyPatch) -> None:
    vision = QwenVision(Settings(QWEN_API_KEY="test-key"))

    async def fake_request_content(image_url: str, prompt: str) -> str:
        assert image_url == "https://cdn/outfit.jpg"
        assert "every distinct wearable garment" in prompt
        assert "Do not merge a top and bottom" in prompt
        return """{
          "items": [
            {
              "canonical_name": "Black T-shirt",
              "display_name": "Black T-shirt",
              "category": "top",
              "color_main": "black",
              "confidence_score": 0.94
            },
            {
              "canonical_name": "Beige trousers",
              "display_name": "Beige trousers",
              "category": "bottom",
              "color_main": "beige",
              "confidence_score": 0.91
            }
          ]
        }"""

    monkeypatch.setattr(vision, "_request_content", fake_request_content)

    results = await vision.analyze_outfit("https://cdn/outfit.jpg", "en-US")

    assert [result.category for result in results] == ["top", "bottom"]
    assert [result.canonical_name for result in results] == [
        "Black T-shirt",
        "Beige trousers",
    ]
