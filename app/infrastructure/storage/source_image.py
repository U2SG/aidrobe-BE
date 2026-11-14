"""Read imports only from verified storage references or bounded public HTTP."""

import base64
import io
import uuid
import warnings
from urllib.parse import urlsplit

from PIL import Image

from app.config.settings import Settings
from app.infrastructure.importers.external_http import InvalidExternalSource, fetch_external
from app.infrastructure.storage.object_key import active_object_key, is_owned_object_key, names_internal_storage

_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_IMAGE_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}


class InvalidSourceImage(ValueError):
    """The source reference or image content cannot be used."""


def resolve_import_source_key(url: str | None, user_id: uuid.UUID, settings: Settings) -> str | None:
    """Validate an upload reference before it reaches backend credentials."""
    if url is None:
        return None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.port == 0
            or "\\" in url
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
        ):
            raise InvalidSourceImage("Invalid source image URL")
    except ValueError as exc:
        raise InvalidSourceImage("Invalid source image URL") from exc
    key = active_object_key(url, settings)
    if key is not None:
        if is_owned_object_key(key, user_id):
            return key
        raise InvalidSourceImage("Source image is unavailable")
    if names_internal_storage(url, settings):
        raise InvalidSourceImage("Source image is unavailable")
    return None


def _image_type(data: bytes) -> str:
    if not data or len(data) > _MAX_IMAGE_BYTES:
        raise InvalidSourceImage("Invalid source image size")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                media_type = _IMAGE_TYPES.get(image.format or "")
                if media_type is None:
                    raise InvalidSourceImage("Unsupported source image type")
                image.verify()
                return media_type
    except (OSError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise InvalidSourceImage("Invalid source image contents") from exc


def image_data_uri(data: bytes) -> str:
    media_type = _image_type(data)
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


async def download_external_source_image(url: str, settings: Settings) -> tuple[bytes, str]:
    """Return checked image bytes and their actual media type, never the response URL."""
    try:
        response, _ = await fetch_external(
            url,
            settings,
            timeout=15,
            max_bytes=_MAX_IMAGE_BYTES,
            accepts=lambda content_type: content_type in _IMAGE_TYPES.values() or content_type in {
                "", "application/octet-stream",
            },
        )
    except InvalidExternalSource as exc:
        raise InvalidSourceImage(str(exc)) from exc
    return response.content, _image_type(response.content)


async def load_source_bytes(
    source_url: str | None, source_key: str | None, *, user_id: uuid.UUID | None = None
) -> bytes:
    """Read a trusted saved key, an ownership-checked legacy upload, or an external image."""
    from app.bootstrap.container import object_storage
    from app.config.settings import get_settings

    settings = get_settings()
    key = source_key
    if key is None and user_id is not None:
        key = resolve_import_source_key(source_url, user_id, settings)
    if key is not None:
        if user_id is not None and not is_owned_object_key(key, user_id):
            raise InvalidSourceImage("Source image is unavailable")
        return await object_storage().get_object(key)
    if source_url is None:
        raise InvalidSourceImage("Source image is required")
    data, _ = await download_external_source_image(source_url, settings)
    return data
