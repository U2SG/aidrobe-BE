"""Assemble prompt sections into a single system prompt (BE#66).

Deliberately trivial: an ordered join of already-built section strings with a
stable separator. No fluent builder class — the sections are plain strings the
caller produces from the other modules, and a function keeps the composition
explicit and testable without ceremony.
"""

from __future__ import annotations

from collections.abc import Iterable

_SECTION_SEPARATOR = "\n\n"


def assemble_system_prompt(sections: Iterable[str | None]) -> str:
    """Join non-empty sections in order with a stable separator.

    None / blank sections are dropped so a caller can pass an optional section
    (e.g. preference rules only when present) without conditionals at the call
    site.
    """
    parts = [s.strip() for s in sections if s and s.strip()]
    return _SECTION_SEPARATOR.join(parts)
