import hashlib
import uuid

from app.config.settings import get_settings
from app.infrastructure.storage.source_image import resolve_import_source_key


def source_version(key: str | None, url: str | None) -> str:
    reference = f"key:{key}" if key else f"url:{url}"
    return hashlib.sha256(reference.encode()).hexdigest()


def upload_key(
    url: str | None, user_id: uuid.UUID, *, expected_key: str | None = None
) -> str | None:
    return resolve_import_source_key(url, user_id, get_settings(), expected_key=expected_key)
