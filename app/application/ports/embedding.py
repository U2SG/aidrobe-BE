from typing import Protocol


class EmbeddingPort(Protocol):
    async def embed_image(self, image_url: str) -> list[float]: ...
