import asyncio
from io import BytesIO
from typing import Any, cast

import httpx

from app.config.embedding import CLIP_MODEL_VERSION
from app.config.settings import Settings


class ClipEmbedder:
    """Local CLIP image embedding provider backed by open_clip ViT-B/32."""

    DIM = 512
    MODEL_NAME = "ViT-B-32"
    PRETRAINED = "openai"
    model_version = CLIP_MODEL_VERSION

    def __init__(self, settings: Settings) -> None:
        self.timeout_seconds = settings.CLIP_EMBEDDING_TIMEOUT_SECONDS
        self._model: Any | None = None
        self._preprocess: Any | None = None
        self._torch: Any | None = None

    async def embed_image(self, image_url: str) -> list[float]:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(image_url)
            response.raise_for_status()
            image_bytes = response.content
        return await self.embed_bytes(image_bytes)

    async def embed_bytes(self, image_bytes: bytes) -> list[float]:
        return await asyncio.to_thread(self._embed_image_bytes, image_bytes)

    def _embed_image_bytes(self, image_bytes: bytes) -> list[float]:
        self._load_model()
        assert self._model is not None
        assert self._preprocess is not None
        assert self._torch is not None

        from PIL import Image

        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        image_tensor = self._preprocess(image).unsqueeze(0)

        with self._torch.no_grad():
            features = self._model.encode_image(image_tensor)
            features = features / features.norm(dim=-1, keepdim=True)

        values = cast(list[float], features.squeeze(0).cpu().tolist())
        if len(values) != self.DIM:
            if len(values) < self.DIM:
                values = values + [0.0] * (self.DIM - len(values))
            else:
                values = values[: self.DIM]
        return [float(value) for value in values]

    def _load_model(self) -> None:
        if self._model is not None:
            return

        import open_clip
        import torch

        model, _, preprocess = open_clip.create_model_and_transforms(
            self.MODEL_NAME,
            pretrained=self.PRETRAINED,
        )
        model.eval()
        self._model = model
        self._preprocess = preprocess
        self._torch = torch
