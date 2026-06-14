from typing import Protocol


class ImageGenerationPort(Protocol):
    async def edit_with_mask(
        self, *, base_image: bytes, mask_image: bytes, prompt: str
    ) -> bytes:
        """Repaint only the WHITE region of `mask_image` according to
        `prompt`; the BLACK region (the preserved subject — person + clothes)
        is kept pixel-for-pixel. Returns the generated image bytes.

        This is the hallucination-control contract: the model never repaints
        the subject, so a user's real garments cannot be altered."""
        ...
