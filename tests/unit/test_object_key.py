"""Unit tests for `extract_internal_object_key` — bucket-safe URL parsing.

The reverse-parser must only extract an object key when the URL belongs to the
bucket the signer actually signs against. A URL pointing at a *different*
per-purpose OSS bucket (items/ootd/private/derived) must return None so the read
path falls back to the stored raw `image_url` instead of signing the key against
the signer's default bucket — which would mint a valid-looking URL for the wrong
bucket.
"""

from __future__ import annotations

from app.config.settings import Settings
from app.infrastructure.storage.object_key import extract_internal_object_key


def _oss_settings() -> Settings:
    return Settings(
        APP_ENV="dev",
        LOCAL_STORAGE_BASE_URL="http://localhost:9000/local",
        ALIYUN_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        ALIYUN_OSS_BUCKET="aidrobe-items",
        ALIYUN_OSS_BUCKET_ITEMS="aidrobe-items",
        ALIYUN_OSS_BUCKET_OOTD="aidrobe-ootd",
        ALIYUN_OSS_BUCKET_PRIVATE="aidrobe-private",
        ALIYUN_OSS_BUCKET_DERIVED="aidrobe-derived",
    )


def test_oss_url_same_bucket_returns_key() -> None:
    settings = _oss_settings()
    url = "https://aidrobe-items.oss-cn-hangzhou.aliyuncs.com/users/u1/x.jpg"
    assert extract_internal_object_key(url, settings) == "users/u1/x.jpg"


def test_oss_url_different_bucket_returns_none() -> None:
    """URL is on a per-purpose bucket the signer does NOT sign against → fall back."""
    settings = _oss_settings()
    url = "https://aidrobe-ootd.oss-cn-hangzhou.aliyuncs.com/users/u1/x.jpg"
    assert extract_internal_object_key(url, settings) is None


def test_oss_url_path_style_same_bucket_returns_key() -> None:
    settings = _oss_settings()
    url = "https://oss-cn-hangzhou.aliyuncs.com/aidrobe-items/users/u1/x.jpg"
    assert extract_internal_object_key(url, settings) == "users/u1/x.jpg"


def test_oss_url_path_style_different_bucket_returns_none() -> None:
    settings = _oss_settings()
    url = "https://oss-cn-hangzhou.aliyuncs.com/aidrobe-derived/users/u1/x.jpg"
    assert extract_internal_object_key(url, settings) is None


def test_local_storage_url_unchanged() -> None:
    settings = _oss_settings()
    url = "http://localhost:9000/local/files/wardrobe/abc/shirt.jpg"
    assert extract_internal_object_key(url, settings) == "wardrobe/abc/shirt.jpg"


def test_s3_virtual_host_same_bucket_returns_key() -> None:
    settings = Settings(
        APP_ENV="dev",
        S3_BUCKET="aidrobe-s3",
        S3_REGION="us-east-1",
    )
    url = "https://aidrobe-s3.s3.us-east-1.amazonaws.com/users/u1/x.jpg"
    assert extract_internal_object_key(url, settings) == "users/u1/x.jpg"


def test_external_url_returns_none() -> None:
    settings = _oss_settings()
    assert extract_internal_object_key("https://cdn.example.com/raw.jpg", settings) is None


def test_none_url_returns_none() -> None:
    assert extract_internal_object_key(None, _oss_settings()) is None
