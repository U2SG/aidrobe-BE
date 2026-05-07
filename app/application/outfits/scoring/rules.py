"""Loader for scoring_rules.yaml and freshness_rules.yaml."""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_SCORING_PATH = Path(__file__).resolve().parents[3] / "config" / "scoring_rules.yaml"
_FRESHNESS_PATH = Path(__file__).resolve().parents[3] / "config" / "freshness_rules.yaml"


@lru_cache(maxsize=1)
def get_scoring_rules() -> dict[str, Any]:
    data = yaml.safe_load(_SCORING_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid scoring rules at {_SCORING_PATH}")
    return data


@lru_cache(maxsize=1)
def get_freshness_rules() -> dict[str, Any]:
    data = yaml.safe_load(_FRESHNESS_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid freshness rules at {_FRESHNESS_PATH}")
    return data


def reload_for_tests() -> None:
    get_scoring_rules.cache_clear()
    get_freshness_rules.cache_clear()
