import hashlib
import struct


class FakeEmbedding:
    """Deterministic mock embedding for dev/test.

    Returns a 512-dim unit vector derived from the image_url hash.
    Same URL always produces the same vector, enabling deterministic tests.
    """

    DIM = 512
    model_version = "fake:512"

    async def embed_image(self, image_url: str) -> list[float]:
        digest = hashlib.sha256(image_url.encode()).digest()
        # Generate 512 floats from the 32-byte digest using a simple PRNG
        seed = struct.unpack(">8I", digest[:32])
        state: int = seed[0]
        vector: list[float] = []
        for _ in range(self.DIM):
            # Linear congruential generator
            state = (state * 1103515245 + 12345) & 0x7FFFFFFF
            value = (state / 0x7FFFFFFF) * 2.0 - 1.0
            vector.append(value)
        # Normalize to unit length
        norm = sum(v * v for v in vector) ** 0.5
        return [v / norm for v in vector]
