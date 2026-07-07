"""BE#87 — unit tests for the copy safety guard.

Placed in ``tests/unit/`` (no DB, no async fixtures) to match the sibling
``test_llm_sanitizer.py`` pattern.  Note: the task spec listed
``tests/application/test_copy_guard.py`` but that directory does not exist and
this file's content is a pure-unit test — no new fixtures are needed.
"""

from __future__ import annotations

import pytest

from app.application.safety.copy_guard import (
    FORBIDDEN_COPY_TERMS,
    find_forbidden_terms,
    sanitize_generated_copy,
)

# ---------------------------------------------------------------------------
# FORBIDDEN_COPY_TERMS coverage — each seed term must be detected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "term",
    [
        "显瘦",
        "遮肉",
        "显胖",
        "藏肉",
        "身材缺陷",
        "减龄",
        "中年",
        "适合你的年龄",
        "显年轻",
        "大妈",
        "老气",
    ],
)
def test_find_forbidden_terms_detects_each_seed(term: str) -> None:
    """Every term in the seed list must trigger when it appears in text."""
    assert term in FORBIDDEN_COPY_TERMS, f"{term!r} missing from FORBIDDEN_COPY_TERMS"
    result = find_forbidden_terms(f"这件衣服{term}非常好看")
    assert term in result


def test_find_forbidden_terms_detects_multiple_hits() -> None:
    result = find_forbidden_terms("显瘦又减龄的穿搭")
    assert "显瘦" in result
    assert "减龄" in result
    assert len(result) == 2


def test_find_forbidden_terms_substring_match() -> None:
    """Substring match — term embedded in a longer sentence."""
    result = find_forbidden_terms("适合中年女性的职场穿搭")
    assert "中年" in result


# ---------------------------------------------------------------------------
# Allowed words must pass through unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "clean_text",
    [
        "舒服又得体的日常穿搭",
        "宽松版型更稳妥",
        "合身的外套适合今天",
        "修身剪裁，干净利落",
        "宽松",
        "合身",
        "得体",
        "舒服",
        "更稳妥",
    ],
)
def test_find_forbidden_terms_allows_clean_words(clean_text: str) -> None:
    assert find_forbidden_terms(clean_text) == []


# ---------------------------------------------------------------------------
# sanitize_generated_copy behaviour
# ---------------------------------------------------------------------------


def test_sanitize_returns_original_when_clean() -> None:
    text = "经典颜色搭配，适合日常通勤"
    result = sanitize_generated_copy(text, fallback="fallback文案")
    assert result == text


def test_sanitize_returns_fallback_when_forbidden() -> None:
    text = "这件衣服显瘦效果很好"
    fallback = "经典颜色搭配，适合日常通勤"
    result = sanitize_generated_copy(text, fallback=fallback)
    assert result == fallback


def test_sanitize_returns_fallback_verbatim() -> None:
    """Fallback is returned exactly as supplied — no trimming."""
    fallback = "  有点空格的 fallback  "
    result = sanitize_generated_copy("显胖的穿搭建议", fallback=fallback)
    assert result == fallback


def test_sanitize_empty_text_is_clean() -> None:
    """Empty string has no forbidden terms — returned as-is."""
    result = sanitize_generated_copy("", fallback="fallback")
    assert result == ""


def test_sanitize_logs_warning_on_hit(capsys: pytest.CaptureFixture[str]) -> None:
    """A structured warning must be emitted when a forbidden term is detected.
    structlog in this repo writes directly to stdout (not stdlib logging),
    so we capture stdout with capsys."""
    sanitize_generated_copy("老气的搭配", fallback="clean fallback")
    captured = capsys.readouterr()
    assert "copy_guard.forbidden_terms_detected" in captured.out


def test_sanitize_no_log_when_clean(capsys: pytest.CaptureFixture[str]) -> None:
    sanitize_generated_copy("简洁大方的穿搭", fallback="fallback")
    captured = capsys.readouterr()
    assert "copy_guard" not in captured.out


# ---------------------------------------------------------------------------
# find_forbidden_terms edge cases
# ---------------------------------------------------------------------------


def test_find_forbidden_terms_returns_empty_list_for_clean() -> None:
    assert find_forbidden_terms("适合日常出行的搭配") == []


def test_find_forbidden_terms_no_duplicates_per_term() -> None:
    """Each matching term appears exactly once even if it occurs twice in text."""
    result = find_forbidden_terms("显瘦显瘦两次")
    assert result.count("显瘦") == 1
