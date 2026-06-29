"""Localized wardrobe item labels and deterministic display-name fallback."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from app.common.locale import lang_subtag, resolve_locale

COLOR_LABELS: Final[dict[str, dict[str, str]]] = {
    "zh-CN": {
        "white": "白色",
        "black": "黑色",
        "beige": "米色",
        "blue": "蓝色",
        "brown": "棕色",
        "gray": "灰色",
        "green": "绿色",
        "red": "红色",
        "pink": "粉色",
        "purple": "紫色",
        "yellow": "黄色",
        "orange": "橙色",
        "navy": "藏青色",
        "cream": "奶油色",
    },
    "en-US": {
        "white": "White",
        "black": "Black",
        "beige": "Beige",
        "blue": "Blue",
        "brown": "Brown",
        "gray": "Gray",
        "green": "Green",
        "red": "Red",
        "pink": "Pink",
        "purple": "Purple",
        "yellow": "Yellow",
        "orange": "Orange",
        "navy": "Navy",
        "cream": "Cream",
    },
}

CATEGORY_LABELS: Final[dict[str, dict[str, str]]] = {
    "zh-CN": {
        "top": "上衣",
        "bottom": "下装",
        "outerwear": "外套",
        "dress": "连衣裙",
        "shoes": "鞋",
        "accessory": "配饰",
        "other": "单品",
    },
    "en-US": {
        "top": "top",
        "bottom": "bottoms",
        "outerwear": "outerwear",
        "dress": "dress",
        "shoes": "shoes",
        "accessory": "accessory",
        "other": "item",
    },
}

_KNOWN_TERMS: Final[dict[str, dict[str, str]]] = {
    "sneaker": {"zh-CN": "运动鞋", "en-US": "sneakers"},
    "sneakers": {"zh-CN": "运动鞋", "en-US": "sneakers"},
    "loafer": {"zh-CN": "乐福鞋", "en-US": "loafers"},
    "loafers": {"zh-CN": "乐福鞋", "en-US": "loafers"},
    "boot": {"zh-CN": "靴子", "en-US": "boots"},
    "boots": {"zh-CN": "靴子", "en-US": "boots"},
    "sandal": {"zh-CN": "凉鞋", "en-US": "sandals"},
    "sandals": {"zh-CN": "凉鞋", "en-US": "sandals"},
    "heel": {"zh-CN": "高跟鞋", "en-US": "heels"},
    "heels": {"zh-CN": "高跟鞋", "en-US": "heels"},
}


def localized_item_name(
    *,
    title: str | None,
    color_main: str | None,
    category: str | None,
    locale: str | None,
    localized_names: Mapping[str, object] | None,
    name_source: str | None,
) -> str:
    """Resolve the display name for a wardrobe item.

    Priority is intentionally fixed:
    1. user/custom title;
    2. localized_names exact locale, then language subtag;
    3. deterministic color + category/known-term fallback;
    4. raw title.
    """
    clean_title = _clean(title)
    if name_source in {"user", "custom"} and clean_title:
        return clean_title

    resolved_locale = resolve_locale(locale)
    localized = _localized_name(localized_names, resolved_locale)
    if localized:
        return localized

    fallback = _deterministic_name(
        title=clean_title,
        color_main=color_main,
        category=category,
        locale=resolved_locale,
    )
    if fallback:
        return fallback
    return clean_title or ""


def _localized_name(
    localized_names: Mapping[str, object] | None, locale: str
) -> str | None:
    if localized_names is None:
        return None
    for key in (locale, lang_subtag(locale)):
        value = localized_names.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _deterministic_name(
    *,
    title: str | None,
    color_main: str | None,
    category: str | None,
    locale: str,
) -> str | None:
    color = _label(COLOR_LABELS, color_main, locale)
    noun = _known_term(title, locale) or _label(CATEGORY_LABELS, category, locale)
    if noun is None:
        return None
    # A localized noun alone still beats leaking the raw English title when the
    # color is unknown (e.g. ivory/khaki not in COLOR_LABELS).
    if color is None:
        return noun
    if locale == "zh-CN":
        return f"{color}{noun}"
    return f"{color} {noun}"


def _known_term(title: str | None, locale: str) -> str | None:
    if not title:
        return None
    words = {part.strip(" -_/.,").lower() for part in title.split()}
    for word in words:
        localized = _KNOWN_TERMS.get(word)
        if localized is not None:
            return localized[locale]
    return None


def _label(
    labels: Mapping[str, Mapping[str, str]], value: str | None, locale: str
) -> str | None:
    if value is None:
        return None
    return labels[locale].get(value.strip().lower())


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
