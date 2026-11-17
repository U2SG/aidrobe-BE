"""Normalize the two persisted shapes of garment style tags."""


def normalize_style_tags(raw: object) -> tuple[str, ...]:
    tags = raw.get("tags") if isinstance(raw, dict) else raw
    if not isinstance(tags, list):
        return ()
    return tuple(tag.strip().lower() for tag in tags if isinstance(tag, str) and tag.strip())
