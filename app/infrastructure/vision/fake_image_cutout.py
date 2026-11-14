"""Test/dev cutout provider — returns the source image unchanged.

Lets the full async cutout pipeline (enqueue → drain → upload → item update)
run end-to-end without an OPENAI_API_KEY or any spend. The item's cutout just
ends up identical to its original, which is the correct no-op behavior."""

from __future__ import annotations


class FakeImageCutout:
    async def extract(
        self, image_bytes: bytes, category: str, *, title: str | None = None, color: str | None = None
    ) -> bytes:
        return image_bytes
