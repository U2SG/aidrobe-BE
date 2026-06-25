"""Helpers for recognizing URLs produced by our object-storage backends."""

from __future__ import annotations

from urllib.parse import unquote, urlparse

from app.config.settings import Settings


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
