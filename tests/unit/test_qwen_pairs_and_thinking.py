from app.application.ports.vision import VisionResult
from app.infrastructure.llm.qwen_llm import _thinking_off
from app.infrastructure.vision.qwen_vision import build_outfit_vision_prompt, merge_paired_garments


def _shoe(box: tuple[float, float, float, float] | None, confidence: float = 0.9, color: str = "green") -> VisionResult:
    return VisionResult(
        title="浅绿色帆布鞋",
        category="shoes",
        color_main=color,
        confidence_score=confidence,
        canonical_name="sneakers",
        display_name="浅绿色帆布鞋",
        bbox=box,
    )


def test_left_and_right_shoe_become_one_pair() -> None:
    # Observed on qwen3-vl-plus (2026-11-14): one pair reported once per foot.
    top = VisionResult(title="T恤", category="top", color_main="blue", confidence_score=0.98)
    merged = merge_paired_garments(
        [top, _shoe((0.36, 0.85, 0.45, 0.92), 0.95), _shoe((0.58, 0.85, 0.67, 0.92), 0.97)]
    )

    assert [r.category for r in merged] == ["top", "shoes"]
    assert merged[1].bbox == (0.36, 0.85, 0.67, 0.92)
    assert merged[1].confidence_score == 0.97


def test_different_shoes_stay_separate() -> None:
    merged = merge_paired_garments([_shoe((0.1, 0.8, 0.2, 0.9)), _shoe((0.6, 0.8, 0.7, 0.9), color="black")])
    assert len(merged) == 2


def test_missing_box_keeps_the_other() -> None:
    merged = merge_paired_garments([_shoe(None), _shoe((0.5, 0.8, 0.6, 0.9))])
    assert len(merged) == 1
    assert merged[0].bbox == (0.5, 0.8, 0.6, 0.9)


def test_outfit_prompt_says_a_pair_is_one_item() -> None:
    assert "A pair" in build_outfit_vision_prompt("zh-CN")


def test_thinking_is_disabled_only_for_qwen3_models() -> None:
    assert _thinking_off("qwen3.8-flash") == {"enable_thinking": False}
    assert _thinking_off("qwen-max") == {}
