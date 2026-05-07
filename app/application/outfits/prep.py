"""Prep step generation: location grouping + material care notes."""

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import structlog

from app.common.i18n import make_message
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

logger = structlog.get_logger(__name__)

_DELICATE_MATERIALS = {
    "silk": "出门前轻熨",
    "linen": "易皱，出门前蒸汽熨一下",
    "wool": "穿前轻刷",
    "cashmere": "穿前轻刷",
    "leather": "出门前擦拭表面",
}


def compute_prep(items: Sequence[WardrobeItem]) -> dict[str, Any]:
    """Returns a dict ready to persist into `outfits.prep_json` and to surface
    via OutfitResponse: `{steps: [I18nMessage], notes: [I18nMessage]}`."""
    by_location: dict[str, list[WardrobeItem]] = defaultdict(list)
    no_location: list[WardrobeItem] = []
    for it in items:
        loc = (it.location or "").strip()
        if loc:
            by_location[loc].append(it)
        else:
            no_location.append(it)

    steps = []
    for loc, group in sorted(by_location.items()):
        titles = "、".join(it.title for it in group)
        steps.append(
            make_message(
                "prep.fetch",
                f"去{loc}拿{titles}",
                {"location": loc, "items": [it.title for it in group]},
            ).model_dump(mode="json")
        )
    if no_location:
        titles = "、".join(it.title for it in no_location)
        steps.append(
            make_message(
                "prep.fetch_unknown",
                f"找出{titles}",
                {"items": [it.title for it in no_location]},
            ).model_dump(mode="json")
        )

    notes = []
    for it in items:
        material = (it.material or "").lower()
        if material in _DELICATE_MATERIALS:
            note_text = _DELICATE_MATERIALS[material]
            notes.append(
                make_message(
                    f"prep.material.{material}",
                    f"{it.title}：{note_text}",
                    {"item_title": it.title, "material": material},
                ).model_dump(mode="json")
            )

    result = {"steps": steps, "notes": notes}
    logger.info(
        "prep.generated",
        steps_count=len(steps),
        notes_count=len(notes),
        locations=sorted(by_location.keys()),
    )
    return result
