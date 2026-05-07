from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "feedback_tags.yaml"


class FeedbackTag(BaseModel):
    key: str
    label_zh_cn: str
    applies_to: list[Literal["overall", "item"]]


@lru_cache(maxsize=1)
def _load_all() -> dict[str, list[FeedbackTag]]:
    data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(
            f"Invalid feedback tag config at {_CONFIG_PATH}: expected a YAML mapping"
        )
    return {scene: [FeedbackTag(**t) for t in tags] for scene, tags in data.items()}


def get_tags_for_scene(scene: str | None) -> list[FeedbackTag]:
    all_tags = _load_all()
    default = all_tags.get("default", [])
    if scene is None:
        return list(default)
    # Normalize: trim, lowercase, cap length defensively (router enforces 32 but
    # keep this layer self-defending against direct callers).
    normalized = scene.strip().lower()[:32]
    if not normalized:
        return list(default)
    extra = all_tags.get(normalized, [])
    seen_keys = {t.key for t in extra}
    return list(extra) + [t for t in default if t.key not in seen_keys]


def reload_for_tests() -> None:
    _load_all.cache_clear()
