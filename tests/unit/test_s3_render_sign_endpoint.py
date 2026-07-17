"""Read-path presigns must be generated against the device-reachable host.

The S3 GET signature binds the host, so a URL signed against the in-cluster
endpoint (minio:9000) returns 403 after any host rewrite — the signing client
itself has to target the public endpoint (verified empirically against the
dev MinIO).
"""

from __future__ import annotations

from app.config.settings import Settings
from app.infrastructure.storage.s3_storage import S3ObjectStorage, _public_presign_endpoint


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "APP_ENV": "dev",
        "S3_BUCKET": "aidrobe",
        "S3_ENDPOINT_URL": "http://minio:9000",
        "S3_REGION": "us-east-1",
        "S3_ACCESS_KEY_ID": "k",
        "S3_SECRET_ACCESS_KEY": "s",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_public_presign_endpoint_strips_path_style_bucket_suffix() -> None:
    # S3_PUBLIC_BASE_URL is a *file* URL prefix and carries the bucket segment;
    # a boto3 endpoint must not, or the bucket doubles up in signed paths.
    assert (
        _public_presign_endpoint("http://100.89.226.13:9000/aidrobe", "aidrobe")
        == "http://100.89.226.13:9000"
    )
    # Virtual-host style base (no path segment) passes through unchanged.
    assert (
        _public_presign_endpoint("https://cdn.example.com", "aidrobe")
        == "https://cdn.example.com"
    )
    assert _public_presign_endpoint(None, "aidrobe") is None


def test_render_urls_are_signed_against_the_public_endpoint() -> None:
    storage = S3ObjectStorage(
        _settings(S3_PUBLIC_BASE_URL="http://100.89.226.13:9000/aidrobe")
    )
    url = storage.sign_render_url("u1/items/i1/cutout.png")
    assert url.startswith("http://100.89.226.13:9000/aidrobe/u1/items/i1/cutout.png?")
    assert "minio:9000" not in url


def test_render_signing_falls_back_to_internal_endpoint_without_public_base() -> None:
    storage = S3ObjectStorage(_settings())
    url = storage.sign_render_url("u1/items/i1/cutout.png")
    assert url.startswith("http://minio:9000/aidrobe/u1/items/i1/cutout.png?")
