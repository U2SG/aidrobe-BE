"""Tests for embedding providers."""

import pytest

from app.infrastructure.embedding.fake_embedding import FakeEmbedding
from app.infrastructure.embedding.remote_clip_embedding import RemoteClipEmbedding


class TestFakeEmbedding:
    @pytest.mark.asyncio
    async def test_returns_512_dim_vector(self) -> None:
        emb = FakeEmbedding()
        result = await emb.embed_image("https://example.com/image.jpg")
        assert len(result) == 512

    @pytest.mark.asyncio
    async def test_same_url_produces_same_vector(self) -> None:
        emb = FakeEmbedding()
        v1 = await emb.embed_image("https://example.com/a.jpg")
        v2 = await emb.embed_image("https://example.com/a.jpg")
        assert v1 == v2

    @pytest.mark.asyncio
    async def test_different_urls_produce_different_vectors(self) -> None:
        emb = FakeEmbedding()
        v1 = await emb.embed_image("https://example.com/a.jpg")
        v2 = await emb.embed_image("https://example.com/b.jpg")
        assert v1 != v2

    @pytest.mark.asyncio
    async def test_vectors_are_unit_length(self) -> None:
        emb = FakeEmbedding()
        result = await emb.embed_image("https://example.com/image.jpg")
        norm = sum(v * v for v in result) ** 0.5
        assert abs(norm - 1.0) < 1e-9


class TestRemoteClipEmbeddingInit:
    def test_requires_api_key(self) -> None:
        from app.config.settings import Settings

        settings = Settings(CLIP_EMBEDDING_API_KEY=None)
        with pytest.raises(ValueError, match="CLIP_EMBEDDING_API_KEY"):
            RemoteClipEmbedding(settings)
