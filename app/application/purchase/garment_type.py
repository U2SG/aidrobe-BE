"""Coarse garment type ("tee", "turtleneck", "jeans", …) for duplicate checks.

Category + colour alone over-reports duplicates: a black graphic tee and a black
turtleneck base layer are both "black tops" but do different jobs. The type is
read from the recognition's English `canonical_name` and from the item title
(often Chinese), first match wins — so more specific types come first
(turtleneck before tee, jeans before trousers).
"""

from __future__ import annotations

from collections.abc import Iterable

# (type, keywords) — keywords are matched as lowercase substrings.
_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # tops
    ("turtleneck", ("turtleneck", "turtle neck", "mock neck", "高领", "打底")),
    ("hoodie", ("hoodie", "sweatshirt", "卫衣")),
    ("polo", ("polo",)),
    ("tank", ("tank", "camisole", "背心", "吊带")),
    ("knit", ("sweater", "knit", "cardigan", "jumper", "pullover", "针织", "毛衣", "开衫")),
    ("tee", ("t-shirt", "tshirt", "t shirt", "tee", "t恤", "t 恤")),
    ("shirt", ("shirt", "blouse", "oxford", "衬衫", "衬衣")),
    # bottoms
    ("jeans", ("jeans", "denim pants", "牛仔裤")),
    ("shorts", ("shorts", "短裤")),
    ("skirt", ("skirt", "半身裙", "裙裤")),
    ("leggings", ("leggings", "打底裤")),
    ("trousers", ("trousers", "pants", "chinos", "slacks", "西裤", "阔腿裤", "直筒裤", "休闲裤", "长裤")),
    # outerwear
    ("blazer", ("blazer", "suit jacket", "西装")),
    ("down", ("down jacket", "puffer", "羽绒")),
    ("coat", ("coat", "overcoat", "trench", "大衣", "风衣")),
    ("jacket", ("jacket", "bomber", "夹克")),
    # shoes
    ("sneakers", ("sneaker", "trainer", "canvas shoe", "运动鞋", "帆布鞋", "板鞋")),
    ("loafers", ("loafer", "乐福")),
    ("boots", ("boot", "靴")),
    ("sandals", ("sandal", "slipper", "凉鞋", "拖鞋")),
    ("heels", ("heel", "pump", "高跟")),
    ("dress_shoes", ("oxford shoe", "derby", "brogue", "皮鞋")),
    # one-piece
    ("dress", ("dress", "连衣裙")),
)


def garment_type(*texts: str | None) -> str | None:
    """The first type whose keyword appears in any of `texts`, else None."""
    haystacks = [t.strip().lower() for t in texts if t]
    if not haystacks:
        return None
    for kind, keywords in _TYPES:
        if any(keyword in text for text in haystacks for keyword in keywords):
            return kind
    return None


def known(types: Iterable[str | None]) -> bool:
    return all(t is not None for t in types)
