"""i18n message contract for explanations (obstacles, prep, scores).

Server returns a structured `{key, params, fallback_text}` triple instead of
a fully-rendered string. Clients with their own locale bundle MUST prefer
`key + params`; clients without a bundle fall back to `fallback_text`
(server-side zh-CN). This keeps server-side A/B and rule-versioning safe
while leaving display localization to the client.
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class I18nMessage(BaseModel):
    """A renderable explanation token.

    - `key` is a stable dotted identifier (`obstacle.laundry`, `prep.iron`).
      Clients lookup their own locale bundle by this key.
    - `params` is a flat string-keyed dict; values must be JSON-serializable
      and already client-safe (no internal IDs, no PII).
    - `fallback_text` is the server-rendered zh-CN string. REQUIRED.
      Clients without a locale bundle for `key` display this verbatim.
    """

    key: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    fallback_text: str = Field(min_length=1, max_length=256)


def make_message(
    key: str,
    fallback_text: str,
    params: dict[str, Any] | None = None,
    *,
    rule_version: str | None = None,
) -> I18nMessage:
    """Build an I18nMessage and emit a structlog `i18n.rendered` event.

    `rule_version` is the scoring/obstacle/prep rule version that produced
    this message — included in the log so we can trace post-deploy regressions.
    """
    msg = I18nMessage(key=key, params=params or {}, fallback_text=fallback_text)
    logger.info(
        "i18n.rendered",
        key=key,
        rule_version=rule_version,
        param_keys=sorted(msg.params.keys()),
    )
    return msg
