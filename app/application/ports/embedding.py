from typing import Protocol


class EmbeddingPort(Protocol):
    """Protocol for image embedding providers.

    `model_version` MUST be a stable, deterministic identifier of the model +
    weights actually producing the vector. It becomes part of the
    `idempotency_key` and `item_embeddings.model_name` so swapping models
    triggers a fresh embedding rather than silently corrupting search.
    Format convention: `<family>:<arch>[:<weights>]`, e.g. `clip:ViT-B-32:openai`.
    """

    model_version: str

    async def embed_image(self, image_url: str) -> list[float]: ...
