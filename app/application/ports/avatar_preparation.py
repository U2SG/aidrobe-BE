"""V3-013 — the provider boundary for building canonical avatar assets.

`AvatarPreparationPort` is one of the four port names frozen by contract §6.4,
so the name is not a local choice. Providers (MiniMax or others) are adapters
behind it and "never leak into the domain contract or API responses" — which is
why `provider` / `model_version` / `prompt_version` are returned as data here
and stored on `avatar_versions` as internal columns, rather than being decided
by the caller or surfaced to FE.

**Nothing in this module can describe a body.** That is contract §7 and
V3_SPEC §15 expressed as a type, the same move V3-012 made with
`ports/avatar_detection.py`: `PreparedAvatarAssets` carries image bytes and
provenance strings and has no field a measurement, size, age, weight or
body-shape judgement could be written into. A future adapter that wanted to
return "estimated size M" would have nowhere to put it without editing this
file, which is a review event rather than a silent addition. V3-013's
acceptance 4 ("No API claims sizing or biometric accuracy") is therefore
structural at the boundary, not only asserted at the response schema.

**Development and CI use fake adapters only** (contract §6.4, and the loop's
safety rule that real user photos are never sent to a third-party provider
during development). `FakeAvatarPreparation` is the only implementation in the
tree; production provider selection is an explicit human gate and is out of
scope for P0 (V3-016).
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PreparedAvatarAssets:
    """The canonical renders one preparation run produced, plus its provenance.

    `side_image` is optional because V3_SPEC §7.4 does not require multi-view
    material in the first phase — a side view is the §7.5 progressive
    enhancement, not a gate on the feature working.
    """

    front_image: bytes
    side_image: bytes | None
    provider: str
    model_version: str
    prompt_version: str


class AvatarPreparationPort(Protocol):
    async def prepare(self, *, source_image: bytes) -> PreparedAvatarAssets:
        """Build canonical reference renders from one consented source photo.

        Takes bytes rather than an object key or a URL: the port must not be
        able to reach into storage on its own, so the caller stays the only
        place that decides which object may be read, and no signed URL is ever
        handed to a provider adapter (contract §7).

        Raises on failure. The caller maps that to a failed preparation, which
        leaves the previous version active (contract §4.2) — there is no
        partial-success return shape, because a half-prepared avatar is not a
        thing that may become `ready`.
        """
        ...
