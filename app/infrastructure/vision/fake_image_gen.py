"""In-process fake for ImageGenerationPort — dev/test without DashScope.

Returns the base image unchanged: no real repaint, no network, no cost. Lets
the OOTD-poster worker / endpoint be exercised end-to-end in tests."""

from __future__ import annotations


class FakeImageGeneration:
    async def edit_with_mask(
        self, *, base_image: bytes, mask_image: bytes, prompt: str
    ) -> bytes:
        return base_image
