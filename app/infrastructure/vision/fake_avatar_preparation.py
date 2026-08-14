"""V3-013 — the only `AvatarPreparationPort` adapter in the tree.

Contract §6.4: "Development and CI use fake/recorded adapters only; any
real-provider evaluation is a separately budgeted, explicitly authorized task
(V3-016, human gate). Real user photos are never sent to third-party providers
during development loops." So this adapter is not a stand-in for something
missing — for P0 it *is* the implementation, and adding a real provider is a
human-gated decision rather than a follow-up chore.

**Local, deterministic, and it leaves the machine never.** The transform is a
normalization the real pipeline would also need: decode, drop EXIF (which
carries GPS and device identifiers we have no reason to keep on a derived
asset), letterbox onto a fixed canvas so every canonical render has the same
geometry regardless of what the phone produced, and re-encode as PNG. Given the
same source bytes it yields byte-identical output, which is what lets the
preparation worker's tests assert a real `preparing → ready` transition with no
container, no network and no spend.

**It cannot invent a person.** The output is a normalized copy of the user's own
photo, so a test that sees a canonical render is seeing the real bytes flow
through the port and into storage. What this adapter does *not* prove is that a
production model produces a usable canonical avatar — the same honest limit
V3-012 recorded for `AVATAR_PERSON_DETECTOR=unavailable` and V3-005 for
pgvector-off. That proof belongs to the budgeted provider evaluation.
"""

from __future__ import annotations

import io

from PIL import Image

from app.application.ports.avatar_preparation import PreparedAvatarAssets

#: Canonical canvas for a front render. Portrait 3:4 at a size that is useful as
#: a reference without storing a full-resolution copy of a person's photo
#: (minimum-necessary data, V3_SPEC §15).
CANONICAL_SIZE = (768, 1024)

#: Flat neutral backdrop for the letterbox. Deliberately mid-grey rather than
#: pure white or black so a render is visibly a *canonical asset* and not
#: mistaken for the untouched original.
_BACKDROP = (128, 128, 128)

PROVIDER = "fake"
MODEL_VERSION = "fake-canonical-1"
#: Bump when the transform changes. It is part of the preparation idempotency
#: key, so a bump is what makes already-prepared avatars eligible for a fresh
#: run instead of being permanently pinned to an older pipeline — the same
#: reasoning as the `assessor` component of V3-012's detection key.
PROMPT_VERSION = "v1"


def _normalize(source_image: bytes) -> bytes:
    with Image.open(io.BytesIO(source_image)) as image:
        # `convert` also flattens any alpha channel against the backdrop, so a
        # transparent PNG cannot render as a silhouette on a black background.
        frame = image.convert("RGB")
        frame.thumbnail(CANONICAL_SIZE, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", CANONICAL_SIZE, _BACKDROP)
        offset = (
            (CANONICAL_SIZE[0] - frame.width) // 2,
            (CANONICAL_SIZE[1] - frame.height) // 2,
        )
        canvas.paste(frame, offset)
        buffer = io.BytesIO()
        # No EXIF is carried over: `Image.new` starts clean and nothing copies
        # the source's metadata onto it. `optimize` keeps the output stable
        # across runs for the same input.
        canvas.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


class FakeAvatarPreparation:
    """Deterministic local preparation. No network, no provider, no spend."""

    async def prepare(self, *, source_image: bytes) -> PreparedAvatarAssets:
        return PreparedAvatarAssets(
            front_image=_normalize(source_image),
            # No side render: one consented photo is one view. Synthesizing a
            # second angle from it would be exactly the fabrication the quality
            # rules exist to prevent, and §7.4 does not ask for one.
            side_image=None,
            provider=PROVIDER,
            model_version=MODEL_VERSION,
            prompt_version=PROMPT_VERSION,
        )
