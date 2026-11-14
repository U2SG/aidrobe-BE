"""Helpers for recognizing URLs produced by our object-storage backends."""

from __future__ import annotations

import uuid
from urllib.parse import ParseResult, unquote, urlparse

from app.config.settings import Settings

#: Phase E5 per-purpose buckets (plan §2.9), by settings field. Used only to
#: recognize our own objects in a bucket we cannot resolve a key for — see
#: `names_unresolvable_per_purpose_bucket`.
_PER_PURPOSE_BUCKET_FIELDS = (
    "ALIYUN_OSS_BUCKET_ITEMS",
    "ALIYUN_OSS_BUCKET_OOTD",
    "ALIYUN_OSS_BUCKET_PRIVATE",
    "ALIYUN_OSS_BUCKET_DERIVED",
)


def is_owned_object_key(object_key: str, user_id: uuid.UUID) -> bool:
    """Was this key issued to *this* user by one of our upload surfaces?

    `POST /v1/uploads/presign` issues keys under `users/<user_id>/`, and
    `POST /v1/uploads/sts-token` scopes its temporary credentials to
    `users/<user_id>/<action>/`. A key from anywhere else was never handed to
    this caller, so a request that names one is asking us to read someone
    else's object with backend credentials — the URL's own access controls are
    not in the path at all once we hold a key.

    The predicate lives here, once, because two surfaces need it: V3-004's
    purchase-candidate ingest (`assert_owned_object_key`, which answers 404 so
    the response cannot be used to probe which keys exist) and V3-012's avatar
    candidate detection (which must not answer at all — its host flow is an
    OOTD save that has to succeed). Two copies of a cross-user-read guard would
    be two chances to fix only one of them.

    `..` is rejected outright rather than normalized: no legitimate key we
    issue contains it, and a traversal segment is how a key that starts with
    the right prefix can still address something else.
    """
    prefix = f"users/{user_id}/"
    return object_key.startswith(prefix) and len(object_key) > len(prefix) and _valid_key(object_key)


def _valid_key(key: str) -> bool:
    # Object references are also used by the local filesystem adapter.
    return (
        bool(key)
        and len(key) <= 512
        and ".." not in key
        and "\\" not in key
        and all(ord(char) >= 32 and ord(char) != 127 for char in key)
        and all(part not in {"", "."} for part in key.split("/"))
    )


#: V3-017 — where a static try-on render lands. Under the `users/<user_id>/`
#: prefix `POST /v1/uploads/presign` already issues, so `is_owned_object_key`
#: recognizes it and the account purge's `delete_prefix("users/<id>/")` catch-all
#: sweeps it even if the row is gone (Phase E1 precedent, and the same choice
#: `avatar_preparation_worker` made for canonical renders).
_STATIC_PREVIEW_PREFIX = "tryon/static"


def static_preview_object_key(user_id: uuid.UUID, asset_id: uuid.UUID) -> str:
    """The one key a static try-on render for this asset can occupy.

    Public and shared rather than private to the worker, because two callers
    need the same answer and they must not disagree: the worker writes here,
    and `DeleteAvatarProfile` sweeps here when `purge_derived=true` (contract
    §4.2). The function is total — it answers for an asset whose render has not
    happened yet — which is what lets the deletion path cover a row the worker
    is rendering *right now*, where `outfit_visual_assets.object_key` is still
    NULL but an object may exist a moment later.

    Derived from the asset id alone, so it is stable across attempts: a retry
    overwrites its own render instead of leaving the previous attempt's image
    behind under a second key nothing references.
    """
    return f"users/{user_id}/{_STATIC_PREVIEW_PREFIX}/{asset_id}.png"


def garment_crop_object_key(user_id: uuid.UUID, task_id: uuid.UUID, temp_item_id: str) -> str:
    """Where the crop of one garment from a multi-garment import photo lands.

    Under `users/<user_id>/` for the same ownership / purge reasons as above,
    and derived from (task, candidate) so a retried import job overwrites its
    own crop instead of leaving orphans.
    """
    return f"users/{user_id}/wardrobe/crops/{task_id}/{temp_item_id}.jpg"


def names_unresolvable_per_purpose_bucket(image_url: str | None, settings: Settings) -> bool:
    """Is this one of *our* objects in a bucket we deliberately cannot resolve?

    `extract_internal_object_key` returns None for a URL in a Phase E5
    per-purpose bucket (items/ootd/private/derived) on purpose — resolving it
    would hand a bare key to a signer that signs against `ALIYUN_OSS_BUCKET`,
    producing a valid-looking URL for the wrong bucket. That refusal is
    correct, but it makes `None` cover two very different situations: an
    external URL that is none of our business, and our own photo somewhere the
    read path cannot reach.

    This predicate separates them so the second can be *reported* instead of
    looking like the first. It resolves no key and grants no access; it only
    recognizes the bucket in the host or path, using the same
    scheme-qualified-endpoint assumption as `_key_after_endpoint_bucket`.
    """
    if not image_url or not settings.ALIYUN_OSS_ENDPOINT:
        return False
    parsed = urlparse(image_url)
    endpoint = urlparse(settings.ALIYUN_OSS_ENDPOINT)
    endpoint_path = endpoint.path.rstrip("/")
    for field in _PER_PURPOSE_BUCKET_FIELDS:
        bucket = str(getattr(settings, field, "") or "")
        # A single-bucket dev config that aliases a purpose onto the default
        # bucket IS resolvable, so it is not this case.
        if not bucket or bucket == settings.ALIYUN_OSS_BUCKET:
            continue
        if parsed.netloc == f"{bucket}.{endpoint.netloc}":
            return True
        if parsed.netloc == endpoint.netloc and parsed.path.startswith(f"{endpoint_path}/{bucket}/"):
            return True
    return False


def extract_internal_object_key(
    image_url: str | None, settings: Settings
) -> str | None:
    """Return an object key only for known backend-generated storage URLs."""
    if not image_url:
        return None

    for provider in ("local", "s3", "aliyun_oss"):
        key = _key_for_provider(image_url, settings, provider)
        if key:
            return key
    return None


def active_object_key(image_url: str | None, settings: Settings) -> str | None:
    """Resolve only URLs the configured adapter can actually read and sign."""
    if not image_url:
        return None
    return _key_for_provider(image_url, settings, settings.STORAGE_PROVIDER)


def _key_for_provider(image_url: str, settings: Settings, provider: str) -> str | None:
    if _parsed_url(image_url) is None:
        return None
    if provider == "local":
        return _key_after_base_path(image_url, settings.LOCAL_STORAGE_BASE_URL, required_prefix="files")
    if provider == "s3" and settings.S3_BUCKET:
        key = _key_after_base_path(image_url, settings.S3_PUBLIC_BASE_URL)
        if key is None:
            key = _key_after_endpoint_bucket(image_url, settings.S3_ENDPOINT_URL, settings.S3_BUCKET)
        if key is None:
            for host in ("s3.amazonaws.com", f"s3.{settings.S3_REGION}.amazonaws.com"):
                key = _key_after_endpoint_bucket(image_url, f"https://{host}", settings.S3_BUCKET)
                if key is not None:
                    break
        return key
    if provider == "aliyun_oss" and settings.ALIYUN_OSS_BUCKET:
        return _key_after_base_path(image_url, settings.ALIYUN_OSS_PUBLIC_BASE_URL) or _key_after_endpoint_bucket(
            image_url, _oss_endpoint(settings), settings.ALIYUN_OSS_BUCKET
        )
    return None


def _oss_endpoint(settings: Settings) -> str:
    endpoint = settings.ALIYUN_OSS_ENDPOINT
    return f"https://{endpoint}" if endpoint and "://" not in endpoint else endpoint


def _parsed_url(url: str) -> ParseResult | None:
    # urllib strips some controls before parsing. Never resolve a modified URL.
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        return None
    try:
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None:
            return parsed
    except ValueError:
        pass
    return None


def names_internal_storage(url: str, settings: Settings) -> bool:
    """Identify storage URLs even if their bucket, host or key cannot be resolved."""
    parsed = _parsed_url(url)
    if parsed is None or parsed.hostname is None:
        return False
    host = parsed.hostname.rstrip(".")
    local = _parsed_url(settings.LOCAL_STORAGE_BASE_URL)
    if local and host == local.hostname and parsed.path.startswith(f"{local.path.rstrip('/')}/files"):
        return True

    for base_url in (settings.S3_PUBLIC_BASE_URL, settings.ALIYUN_OSS_PUBLIC_BASE_URL):
        base = _parsed_url(base_url) if base_url else None
        if base and host == base.hostname:
            return True
    for address in (settings.S3_ENDPOINT_URL, _oss_endpoint(settings)):
        endpoint = _parsed_url(address) if address else None
        if endpoint and endpoint.hostname and (host == endpoint.hostname or host.endswith(f".{endpoint.hostname}")):
            return True

    buckets = {
        bucket
        for bucket in (
            settings.S3_BUCKET,
            settings.ALIYUN_OSS_BUCKET,
            *(getattr(settings, field) for field in _PER_PURPOSE_BUCKET_FIELDS),
        )
        if bucket
    }
    # An otherwise unrelated host that embeds our bucket and user namespace
    # is not an alternate object-storage endpoint.
    parts = unquote(parsed.path).split("/")
    return any(
        host.startswith(f"{bucket}.") or (
            bucket in parts and ("users" in parts or host.endswith(".amazonaws.com"))
        )
        for bucket in buckets
    )


def trusted_item_image_key(url: str | None, user_id: uuid.UUID, settings: Settings) -> str | None:
    """Resolve a URL carried by a server-owned wardrobe item for this user."""
    key = active_object_key(url, settings)
    if key and (is_owned_object_key(key, user_id) or key.startswith(f"{user_id}/items/")):
        return key
    return None


def _key_after_base_path(
    image_url: str,
    base_url: str | None,
    *,
    required_prefix: str | None = None,
) -> str | None:
    if not base_url:
        return None
    parsed = urlparse(image_url)
    base = urlparse(base_url)
    if parsed.scheme != base.scheme or parsed.netloc != base.netloc:
        return None

    base_path = base.path.rstrip("/")
    path = parsed.path
    if base_path and not (path == base_path or path.startswith(f"{base_path}/")):
        return None

    relative = path[len(base_path) :].lstrip("/")
    if required_prefix is not None:
        prefix = f"{required_prefix}/"
        if not relative.startswith(prefix):
            return None
        relative = relative[len(prefix) :]
    return _clean_key(relative)


def _key_after_endpoint_bucket(
    image_url: str,
    endpoint_url: str | None,
    bucket: str,
) -> str | None:
    if not endpoint_url:
        return None
    parsed = urlparse(image_url)
    endpoint = urlparse(endpoint_url)
    if parsed.scheme != endpoint.scheme:
        return None

    endpoint_path = endpoint.path.rstrip("/")
    path_style_prefix = f"{endpoint_path}/{bucket}".rstrip("/")
    if parsed.netloc == endpoint.netloc and parsed.path.startswith(
        f"{path_style_prefix}/"
    ):
        return _clean_key(parsed.path[len(path_style_prefix) :].lstrip("/"))

    virtual_host = f"{bucket}.{endpoint.netloc}"
    if parsed.netloc == virtual_host and (
        not endpoint_path or parsed.path.startswith(f"{endpoint_path}/")
    ):
        return _clean_key(parsed.path[len(endpoint_path) :].lstrip("/"))

    return None


def _clean_key(value: str) -> str | None:
    key = unquote(value)
    return key if _valid_key(key) else None
