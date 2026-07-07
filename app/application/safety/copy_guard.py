"""BE#87 — safety guard: prevent LLM-generated user-facing copy from
containing body-shaming or age-anxiety language.

Design decisions:
- Single source of truth: ``FORBIDDEN_COPY_TERMS`` tuple, extensible here only.
- Substring match is correct for Chinese (no word boundaries to split on).
- No in-place rewriting: deleting a Chinese word mid-sentence can flip or
  fragment the meaning. The safe action is to fall back to a vetted neutral
  string.
- ``sanitize_generated_copy`` only logs + returns fallback; it never raises.
  The caller always receives a displayable string.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Term list — single source of truth, one entry per intent
# ---------------------------------------------------------------------------

#: Seed list from BE#87 plus close anxiety-frame variants.
#: Add terms here; both ``find_forbidden_terms`` and ``sanitize_generated_copy``
#: pick up changes automatically.
#: Functional garment words (宽松, 合身, 修身, …) are NOT listed here.
FORBIDDEN_COPY_TERMS: tuple[str, ...] = (
    # Body-shaming / figure-anxiety framing
    "显瘦",
    "遮肉",
    "显胖",
    "藏肉",
    "身材缺陷",
    # Age-anxiety framing
    "减龄",
    "中年",
    "适合你的年龄",
    "显年轻",
    "大妈",
    "老气",
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def find_forbidden_terms(text: str) -> list[str]:
    """Return every term from ``FORBIDDEN_COPY_TERMS`` found in *text*.

    Performs a simple substring scan — appropriate for Chinese text where
    word boundaries are implicit.  Returns an empty list when the text is
    clean.

    >>> find_forbidden_terms("这件衣服显瘦又减龄")
    ['显瘦', '减龄']
    >>> find_forbidden_terms("舒适宽松的日常搭配")
    []
    """
    return [term for term in FORBIDDEN_COPY_TERMS if term in text]


def sanitize_generated_copy(text: str, *, fallback: str) -> str:
    """Return *text* unchanged when it is clean; return *fallback* verbatim
    when *text* contains any forbidden term.

    Logs a structured warning so the incident is observable in production
    without exposing the offending phrase to the user.

    :param text:     LLM-generated candidate string.
    :param fallback: Vetted neutral copy supplied by the caller.  Must be
                     safe — this function does **not** re-check the fallback.
    :returns:        *text* or *fallback*.
    """
    hits = find_forbidden_terms(text)
    if not hits:
        return text
    logger.warning(
        "copy_guard.forbidden_terms_detected",
        hit_count=len(hits),
        terms=hits,
        fallback_used=fallback,
    )
    return fallback
