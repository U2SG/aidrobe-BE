"""Phase E4 — LLM input sanitizer + user-token hash."""

from __future__ import annotations

import uuid

import pytest

from app.config.settings import Settings, get_settings
from app.infrastructure.llm import sanitizer


@pytest.fixture(autouse=True)
def _override_settings_to_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a known-good HMAC key for hash-determinism asserts."""
    fixed = Settings(
        APP_ENV="dev",
        LLM_USER_TOKEN_HMAC_KEY="0123456789abcdef0123456789abcdef",  # 32 bytes
    )
    monkeypatch.setattr("app.infrastructure.llm.sanitizer.get_settings", lambda: fixed)
    get_settings.cache_clear()


def test_hash_user_token_is_deterministic() -> None:
    uid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    assert sanitizer.hash_user_token(uid) == sanitizer.hash_user_token(uid)


def test_hash_user_token_differs_per_user() -> None:
    a = sanitizer.hash_user_token(uuid.UUID("11111111-1111-1111-1111-111111111111"))
    b = sanitizer.hash_user_token(uuid.UUID("22222222-2222-2222-2222-222222222222"))
    assert a != b


def test_hash_user_token_format() -> None:
    """`usr_<22 base64url chars>` — 22 chars from 16 bytes / 6 = 21.33, pad
    to 22. No padding `=` (we strip)."""
    token = sanitizer.hash_user_token(uuid.uuid4())
    assert token.startswith("usr_")
    assert len(token) == 4 + 22  # 4-char prefix + 22 b64url chars
    assert "=" not in token
    assert "+" not in token  # base64url, not standard b64
    assert "/" not in token


def test_hash_user_token_does_not_leak_uuid() -> None:
    uid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    token = sanitizer.hash_user_token(uid)
    # Token MUST NOT contain the user_id substring.
    assert str(uid) not in token
    assert "11111111" not in token


def test_hash_user_token_changes_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rotating the HMAC key changes every user's token. Documented as
    "don't rotate unless you accept invalidating bindings"."""
    uid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    a = sanitizer.hash_user_token(uid)

    rotated = Settings(APP_ENV="dev", LLM_USER_TOKEN_HMAC_KEY="X" * 32)
    monkeypatch.setattr("app.infrastructure.llm.sanitizer.get_settings", lambda: rotated)
    b = sanitizer.hash_user_token(uid)
    assert a != b


def test_hash_user_token_raises_on_short_key(monkeypatch: pytest.MonkeyPatch) -> None:
    short = Settings(APP_ENV="dev", LLM_USER_TOKEN_HMAC_KEY="too-short")
    monkeypatch.setattr("app.infrastructure.llm.sanitizer.get_settings", lambda: short)
    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        sanitizer.hash_user_token(uuid.uuid4())


def test_hash_user_token_raises_on_prod_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hard refusal in prod with the dev placeholder key — anyone with the
    source could otherwise recompute every user's token."""
    placeholder = Settings(
        APP_ENV="prod",
        LLM_USER_TOKEN_HMAC_KEY="dev-only-change-me-llm-user-token-hmac-key-32b",
        AUTH_APPLE_VERIFY_SIGNATURE=False,
    )
    monkeypatch.setattr("app.infrastructure.llm.sanitizer.get_settings", lambda: placeholder)
    with pytest.raises(RuntimeError, match="must be configured in prod"):
        sanitizer.hash_user_token(uuid.uuid4())


def test_sanitize_drops_non_whitelisted_fields() -> None:
    raw = {
        "category": "top",
        "color": "white",
        "title": "Mom's birthday gift shirt",  # potentially personal
        "user_id": "real-id-leaks",  # MUST be stripped
        "image_url": "https://cdn/private.jpg",  # MUST be stripped
        "email": "user@example.com",
    }
    out = sanitizer.sanitize_for_llm(raw)
    assert out == {"category": "top", "color": "white"}


def test_sanitize_recurses_into_lists_and_dicts() -> None:
    raw = {
        "items": [
            {"category": "top", "title": "secret name"},
            {"category": "bottom", "color": "navy", "user_notes": "private"},
        ],
        "meta": {"category": "outerwear", "owner_id": "leak"},
    }
    # Top-level "items" / "meta" aren't whitelisted, but recursion only
    # applies to nested dicts/lists THAT ARE WHITELISTED. Top-level drops
    # them entirely → empty dict.
    assert sanitizer.sanitize_for_llm(raw) == {}


def test_sanitize_preserves_whitelisted_lists() -> None:
    raw = {"season_tags": ["spring", "fall"], "evil": "drop"}
    assert sanitizer.sanitize_for_llm(raw) == {"season_tags": ["spring", "fall"]}


def test_attach_user_token_combines_filter_and_hash() -> None:
    uid = uuid.UUID("33333333-3333-3333-3333-333333333333")
    raw = {"category": "top", "user_id": str(uid)}
    out = sanitizer.attach_user_token(raw, uid)
    assert out["category"] == "top"
    assert "user_id" not in out
    assert out["usr"].startswith("usr_")
    # Same call twice gives same opaque token.
    assert sanitizer.attach_user_token(raw, uid)["usr"] == out["usr"]
