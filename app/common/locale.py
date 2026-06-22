"""Locale resolution shared across Vision / Ask / settings (BE#30).

The product default is Simplified Chinese: every server-side i18n
`fallback_text` is zh-CN and the Ask preambles default to Chinese. So the
*effective* locale must resolve to `zh-CN` when nothing is specified.

User settings persist one of three values — `zh-CN`, `en-US`, or `system`.
`system` means "no explicit choice"; it resolves to the product default.
Resolution precedence is: request locale > user-settings locale > default.
"""

from __future__ import annotations

from typing import Final, Literal

LocaleSetting = Literal["zh-CN", "en-US", "system"]

# Stored/settable values. `system` is the safe default — defer to the product
# default at resolution time rather than freezing a choice the user never made.
SETTING_VALUES: Final[frozenset[str]] = frozenset({"zh-CN", "en-US", "system"})
DEFAULT_SETTING: Final[LocaleSetting] = "system"

# Effective product default once `system` / unknown is resolved.
DEFAULT_LOCALE: Final[str] = "zh-CN"

# Supported display locales (resolved, never `system`).
_SUPPORTED: Final[dict[str, str]] = {"zh": "zh-CN", "en": "en-US"}


def resolve_locale(
    request_locale: str | None = None,
    settings_locale: str | None = None,
) -> str:
    """Resolve the effective BCP-47 locale (`zh-CN` / `en-US`).

    Precedence: request hint > persisted user setting > product default.
    A `system` (or empty / unknown) value at any layer falls through to the
    next; if nothing resolves we return :data:`DEFAULT_LOCALE`.
    """
    for candidate in (request_locale, settings_locale):
        resolved = _match(candidate)
        if resolved is not None:
            return resolved
    return DEFAULT_LOCALE


def _match(value: str | None) -> str | None:
    if not value:
        return None
    lang = value.split("-", 1)[0].strip().lower()
    return _SUPPORTED.get(lang)


def lang_subtag(locale: str) -> str:
    """The lowercase language subtag (`zh-CN` -> `zh`)."""
    return locale.split("-", 1)[0].strip().lower()
