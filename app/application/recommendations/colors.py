COLOR_CONFLICTS: set[frozenset[str]] = {
    frozenset(("red", "green")),
    frozenset(("orange", "purple")),
    frozenset(("yellow", "purple")),
}

NEUTRAL_COLORS = {"black", "white", "gray", "grey", "navy", "beige"}


def color_score(colors: list[str]) -> float:
    normalized = [color.lower() for color in colors if color]
    for left in normalized:
        for right in normalized:
            if left != right and frozenset((left, right)) in COLOR_CONFLICTS:
                return 0.2
    if any(color in NEUTRAL_COLORS for color in normalized):
        return 1.0
    return 0.75
