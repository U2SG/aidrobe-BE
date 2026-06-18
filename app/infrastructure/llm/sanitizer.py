"""LLM input sanitizer — Phase E4.

Anything that flows into an LLM prompt MUST go through this module first.
Plan §2.9 whitelist: only `category`, `color`, `status`, `location`,
`season_tags`, plus the opaque `usr_<22ch>` user token derived via HMAC.
Raw user_id, email, image URLs, free-form titles, and anything else are
stripped or refused.

Why opaque user token rather than no identifier at all: the LLM provider
needs *some* per-conversation identity to maintain context across turns
(Ask sessions). A deterministic, non-reversible hash gives us that without
exposing the user_id (which would link prompts to our DB if logs leak).

Best-practice notes baked in:
- HMAC-SHA256 (deterministic, NIST-approved, fast).
- Key MUST live in env, never in code; we hard-fail on prod startup if the
  placeholder default is still in use.
- 16 bytes (128 bits) of output → ~3.4×10^38 collision space; ample for
  every user we'll ever have.
- `usr_` prefix so prompts and logs can grep for it; base64url so it's
  prompt-safe (no `/`, no `+`).
- Single fixed key in this design — rotation is impossible without
  invalidating every existing Ask session, which is fine because Ask
  sessions have short lifetimes. If long-lived bindings appear later,
  switch to a key-versioned format like `usr_v2_<22ch>`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from typing import Any, cast

from app.config.settings import get_settings

# Plan §2.9 whitelist. Adding a field here is a deliberate change — review
# carefully whether it could leak PII. Anything user-typed (titles,
# locations as user-entered strings) is suspect.
LLM_FIELD_WHITELIST: frozenset[str] = frozenset(
    {
        "category",
        "color",
        "color_main",
        "status",
        "location",  # NOTE: free-form. UI prompts should constrain to bucket
        # values like "office" / "home"; if it's user-typed,
        # promote it to free-text and drop here.
        "season_tags",
        "fit_status",
        "fit_intent",
        # opaque user token added by `attach_user_token` if present
        "usr",
    }
)

_PLACEHOLDER_KEY = "dev-only-change-me-llm-user-token-hmac-key-32b"
_TOKEN_PREFIX = "usr_"
_TOKEN_BYTES = 16  # 128 bits


def _resolve_key() -> bytes:
    """Get the HMAC key, hard-failing on prod misconfiguration."""
    settings = get_settings()
    raw = settings.LLM_USER_TOKEN_HMAC_KEY
    if settings.APP_ENV == "prod" and raw == _PLACEHOLDER_KEY:
        # Prod with the placeholder = anyone with the source code can
        # recompute every user's token. Hard refuse.
        raise RuntimeError(
            "LLM_USER_TOKEN_HMAC_KEY must be configured in prod (got placeholder)"
        )
    if len(raw.encode()) < 32:
        # Even in dev we want a key with enough entropy that tests catch
        # accidental short-key configurations.
        raise RuntimeError(
            "LLM_USER_TOKEN_HMAC_KEY must be at least 32 bytes"
        )
    return raw.encode()


def hash_user_token(user_id: uuid.UUID) -> str:
    """Derive the opaque `usr_<22ch>` token for `user_id`.

    Deterministic: same user always gets the same token. Non-reversible:
    knowing the token alone does not reveal user_id without the HMAC key."""
    digest = hmac.new(_resolve_key(), str(user_id).encode(), hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(digest[:_TOKEN_BYTES]).decode().rstrip("=")
    return f"{_TOKEN_PREFIX}{encoded}"


def sanitize_for_llm(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a NEW dict containing only whitelisted fields. Lists/dicts
    are recursed into (so a list of items has each item filtered)."""
    return cast(dict[str, Any], _filter(payload))


def _filter(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _filter(v) for k, v in value.items() if k in LLM_FIELD_WHITELIST}
    if isinstance(value, list):
        return [_filter(v) for v in value]
    return value


def attach_user_token(payload: dict[str, Any], user_id: uuid.UUID) -> dict[str, Any]:
    """Wrap `sanitize_for_llm` and inject the hashed user token. Use this
    for prompts that need cross-turn identity (Ask sessions). Pure prompts
    that do not need identity should call `sanitize_for_llm` directly."""
    out = sanitize_for_llm(payload)
    out["usr"] = hash_user_token(user_id)
    return out
