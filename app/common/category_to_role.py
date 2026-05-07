from typing import Literal

import structlog

log = structlog.get_logger(__name__)

DisplayRole = Literal["top", "outer", "bottom", "shoes", "accessory"]

_MAP: dict[str, DisplayRole] = {
    "top": "top",
    "shirt": "top",
    "t_shirt": "top",
    "tshirt": "top",
    "blouse": "top",
    "knitwear": "top",
    "sweater": "top",
    "hoodie": "top",
    "outerwear": "outer",
    "outer": "outer",
    "jacket": "outer",
    "coat": "outer",
    "blazer": "outer",
    "bottom": "bottom",
    "pants": "bottom",
    "trousers": "bottom",
    "skirt": "bottom",
    "jeans": "bottom",
    "shorts": "bottom",
    "dress": "top",
    "shoes": "shoes",
    "sneakers": "shoes",
    "boots": "shoes",
    "heels": "shoes",
    "sandals": "shoes",
    "bag": "accessory",
    "hat": "accessory",
    "scarf": "accessory",
    "belt": "accessory",
    "jewelry": "accessory",
    "accessory": "accessory",
}


def map_category_to_role(category: str | None) -> DisplayRole:
    if not category:
        log.warning("category.unknown_role", category=category)
        return "accessory"
    key = category.lower().strip()
    role = _MAP.get(key)
    if role is None:
        log.warning("category.unknown_role", category=category)
        return "accessory"
    return role
