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
              "box_2d": [250, 510, 700, 940],
              "confidence_score": 0.91
            }
          ]
        }"""

    monkeypatch.setattr(vision, "_request_content", fake_request_content)

    results = await vision.analyze_outfit("https://cdn/outfit.jpg", "en-US")

    assert [result.category for result in results] == ["top", "bottom"]
    assert results[1].bbox == (0.25, 0.51, 0.7, 0.94)
    assert [result.canonical_name for result in results] == [
        "Black T-shirt",
        "Beige trousers",
    ]


async def test_outfit_review_rejects_only_on_garment_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    vision = QwenVision(Settings(QWEN_API_KEY="test"))
    prompts: list[str] = []

    async def fake_request_content(image_url: str, prompt: str, *references: str) -> str:
        prompts.append(prompt)
        return (
            '{"composition_matches": true, "garment_matches": [true, true],'
            ' "minor_differences": ["pocket stitching differs", 3]}'
        )

    monkeypatch.setattr(vision, "_request_content", fake_request_content)
    review = await vision.matches_outfit_photo([b"a", b"b"], b"g", garments=["top: Tee", "bottom: Jeans"])

    assert review == {
        "accepted": True,
        "composition_matches": True,
        "garment_matches": [True, True],
        "minor_differences": ["pocket stitching differs"],
    }
    prompt = prompts[0].lower()
    assert "uncertainty" not in prompt
    for identity_change in ("material family", "matte versus glossy", "cardigan versus pullover", "prominent text"):
        assert identity_change in prompt


async def test_footwear_review_reports_design_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    vision = QwenVision(Settings(QWEN_API_KEY="test"))

    async def fake_request_content(image_url: str, prompt: str, *references: str) -> str:
        return '{"same_design": false, "minor_differences": ["boot instead of sneaker"]}'

    monkeypatch.setattr(vision, "_request_content", fake_request_content)

    assert await vision.matches_outfit_footwear(b"s", b"g") == {
        "accepted": False,
        "minor_differences": ["boot instead of sneaker"],
    }


async def test_footwear_review_requires_boolean_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    vision = QwenVision(Settings(QWEN_API_KEY="test"))

    async def fake_request_content(image_url: str, prompt: str, *references: str) -> str:
        return '{"same_design": "yes"}'

    monkeypatch.setattr(vision, "_request_content", fake_request_content)

    with pytest.raises(ValueError):
        await vision.matches_outfit_footwear(b"s", b"g")
