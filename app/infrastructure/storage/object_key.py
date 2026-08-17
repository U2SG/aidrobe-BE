"""Helpers for recognizing URLs produced by our object-storage backends."""

from __future__ import annotations

import uuid
from urllib.parse import unquote, urlparse

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
    return object_key.startswith(f"users/{user_id}/") and ".." not in object_key


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

    local_key = _key_after_base_path(
        image_url,
        settings.LOCAL_STORAGE_BASE_URL,
        required_prefix="files",
    )
    if local_key:
        return local_key

    for base_url in (settings.S3_PUBLIC_BASE_URL, settings.ALIYUN_OSS_PUBLIC_BASE_URL):
        key = _key_after_base_path(image_url, base_url)
        if key:
            return key

    # Endpoint/virtual-host URLs embed the bucket name. Only extract a key when
    # that bucket is the one the signer actually signs against — the S3 adapter
    # signs against `S3_BUCKET` and the OSS adapter against `ALIYUN_OSS_BUCKET`.
    # Matching any of the per-purpose OSS buckets (items/ootd/private/derived)
    # would let the read path sign the key against the signer's default bucket,
    # producing a valid-looking URL for the WRONG bucket. Return None instead so
    # the caller falls back to the stored raw `image_url`.
    if settings.S3_BUCKET:
        key = _key_after_endpoint_bucket(
            image_url, settings.S3_ENDPOINT_URL, settings.S3_BUCKET
        )
        if key:
            return key
        key = _key_after_s3_virtual_host(
            image_url, settings.S3_BUCKET, settings.S3_REGION
        )
        if key:
            return key

    if settings.ALIYUN_OSS_BUCKET:
        key = _key_after_endpoint_bucket(
            image_url, settings.ALIYUN_OSS_ENDPOINT, settings.ALIYUN_OSS_BUCKET
        )
        if key:
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


def _key_after_s3_virtual_host(
    image_url: str,
    bucket: str,
    region: str,
) -> str | None:
    parsed = urlparse(image_url)
    expected_host = f"{bucket}.s3.{region}.amazonaws.com"
    if parsed.scheme == "https" and parsed.netloc == expected_host:
        return _clean_key(parsed.path.lstrip("/"))
    return None


def _clean_key(value: str) -> str | None:
    key = unquote(value).lstrip("/")
    return key or None
