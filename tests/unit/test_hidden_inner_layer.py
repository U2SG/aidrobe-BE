"""An inner top boxed inside its jacket is marked hidden, so no product image is invented for it."""

from app.application.ports.vision import VisionResult
from app.infrastructure.workers.import_worker import _hidden_by_outer_layer


def _garment(category: str, bbox: tuple[float, float, float, float] | None) -> VisionResult:
    return VisionResult(title=category, category=category, color_main="white", confidence_score=0.9, bbox=bbox)


def test_top_inside_an_outer_layer_is_hidden() -> None:
    # E1-02: a henley showing only a strip between the fronts of an open cardigan.
    cardigan = _garment("outerwear", (0.425, 0.506, 0.563, 0.681))
    inner = _garment("top", (0.467, 0.515, 0.525, 0.608))
    trousers = _garment("bottom", (0.427, 0.602, 0.559, 0.86))
    garments = [cardigan, inner, trousers]
    assert _hidden_by_outer_layer(inner, garments)
    assert not _hidden_by_outer_layer(cardigan, garments)
    assert not _hidden_by_outer_layer(trousers, garments)


def test_top_beside_or_without_a_jacket_stays_visible() -> None:
    jacket = _garment("outerwear", (0.1, 0.1, 0.4, 0.5))
    assert not _hidden_by_outer_layer(_garment("top", (0.3, 0.1, 0.7, 0.5)), [jacket])  # 25% overlap
    assert not _hidden_by_outer_layer(_garment("top", (0.3, 0.1, 0.7, 0.5)), [])
    assert not _hidden_by_outer_layer(_garment("top", None), [jacket])
