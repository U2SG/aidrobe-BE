"""Deterministic self-critique validators (BE#66).

Pure predicates over plain data — no second LLM call (the issue prefers
deterministic validation). Shared by:
- the Ask runtime, which logs violations post-parse without mutating the answer;
- the recommendation eval harness (`tests/evals/checkers.py` imports from HERE —
  never the reverse, so app code never depends on tests).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# Preference type -> the wardrobe item attribute it constrains. Only the types
# that map cleanly onto a single structured attribute are checked deterministically.
_PREF_TYPE_TO_ATTR: dict[str, str] = {
    "color": "color_main",
    "material": "material",
    "fit": "fit_status",
    "footwear": "sub_category",
}


def unknown_item_refs(referenced_ids: Iterable[Any], known_ids: Iterable[Any]) -> list[str]:
    """Referenced ids that do not exist in the known/allowed set (hallucinations)."""
    known = {str(k) for k in known_ids}
    return sorted({str(r) for r in referenced_ids if str(r) not in known})


def disliked_attribute_offenders(
    items: Iterable[Mapping[str, Any]], attribute: str, disliked_values: Iterable[str]
) -> list[str]:
    """Ids of items whose `attribute` equals a disliked value (case-insensitive).

    Each item is a mapping carrying `id` (or `item_id`) and `attribute`.
    """
    bad = {str(v).strip().lower() for v in disliked_values if str(v).strip()}
    offenders: list[str] = []
    if not bad:
        return offenders
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if str(item.get(attribute, "")).strip().lower() in bad:
            ident = item.get("id", item.get("item_id"))
            if ident is not None:
                offenders.append(str(ident))
    return offenders


def negative_preference_violations(
    items: Iterable[Mapping[str, Any]], negatives: Iterable[Mapping[str, Any]]
) -> list[str]:
    """Human-readable violations: referenced items carrying an attribute the user
    explicitly dislikes. Best-effort + non-mutating — the caller logs the result
    (it never drops items, so a fuzzy preference value cannot suppress an answer).
    """
    item_list = list(items)
    violations: list[str] = []
    for neg in negatives:
        if not isinstance(neg, Mapping):
            continue
        attr = _PREF_TYPE_TO_ATTR.get(str(neg.get("type", "")).strip().lower())
        if attr is None:
            continue
        value = str(neg.get("value", "")).strip()
        for oid in disliked_attribute_offenders(item_list, attr, [value]):
            violations.append(f"item {oid} {attr}={value!r} contradicts a negative preference")
    return violations
