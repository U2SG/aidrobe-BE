"""Model identities shared by embedding producers and vector readers."""

from app.config.settings import get_settings

CLIP_MODEL_VERSION = "clip:ViT-B-32:openai"
FAKE_MODEL_VERSION = "fake:512"
REMOTE_CLIP_MODEL_VERSION = "remote_clip:512"

_MODEL_VERSIONS = {
    "clip": CLIP_MODEL_VERSION,
    "open_clip": CLIP_MODEL_VERSION,
    "fake": FAKE_MODEL_VERSION,
    "remote_clip": REMOTE_CLIP_MODEL_VERSION,
}


def embedding_model_version() -> str:
    provider = get_settings().EMBEDDING_PROVIDER
    try:
        return _MODEL_VERSIONS[provider]
    except KeyError:
        raise ValueError(f"Unsupported embedding provider: {provider}") from None
