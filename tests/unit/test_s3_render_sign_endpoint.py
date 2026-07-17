"""Read-path presigns must be generated against the device-reachable host.

The S3 GET signature binds the host, so a URL signed against the in-cluster
endpoint (minio:9000) returns 403 after any host rewrite — the signing client
itself has to target the public endpoint (verified empirically against the
dev MinIO). These tests assert the GENERATED URL host for every supported
`S3_PUBLIC_BASE_URL` shape (PR #109 review: endpoint derivation alone missed
botocore re-prepending the bucket on virtual-host bases).
"""

from __future__ import annotations

from urllib.parse import urlparse

from app.config.settings import Settings
from app.infrastructure.storage.s3_storage import S3ObjectStorage, _public_presign_target


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


def test_public_presign_target_maps_each_base_shape() -> None:
    # Path-style file base carries the bucket segment; the endpoint must not.
    assert _public_presign_target("http://100.89.226.13:9000/aidrobe", "aidrobe") == (
        "http://100.89.226.13:9000",
        "path",
    )
    # Virtual-host base: drop the bucket label so botocore's virtual
    # addressing re-adds it exactly once.
    assert _public_presign_target("https://aidrobe.s3.us-east-1.amazonaws.com", "aidrobe") == (
        "https://s3.us-east-1.amazonaws.com",
        "virtual",
    )
    # Opaque CDN host (bucket hidden) can't be an S3 signing target.
    assert _public_presign_target("https://cdn.example.com", "aidrobe") is None
    assert _public_presign_target("https://cdn.example.com/media", "aidrobe") is None
    assert _public_presign_target(None, "aidrobe") is None


def test_render_urls_are_signed_against_the_path_style_public_endpoint() -> None:
    storage = S3ObjectStorage(
        _settings(S3_PUBLIC_BASE_URL="http://100.89.226.13:9000/aidrobe")
    )
    url = storage.sign_render_url("u1/items/i1/cutout.png")
    assert url.startswith("http://100.89.226.13:9000/aidrobe/u1/items/i1/cutout.png?")
    assert "minio:9000" not in url


def test_render_urls_preserve_virtual_host_public_bases() -> None:
    # Regression (PR #109 review): passing the full virtual-host base as the
    # endpoint made botocore prepend the bucket again (bucket.bucket.host).
    storage = S3ObjectStorage(
        _settings(S3_PUBLIC_BASE_URL="https://aidrobe.s3.us-east-1.amazonaws.com")
    )
    url = storage.sign_render_url("u1/items/i1/cutout.png")
    assert urlparse(url).netloc == "aidrobe.s3.us-east-1.amazonaws.com"
    assert "aidrobe.aidrobe." not in url


def test_render_signing_falls_back_to_internal_endpoint_when_base_unmappable() -> None:
    # An opaque CDN base can't be signed against — degrade to the internal
    # client (pre-#109 behavior) rather than emit URLs with broken signatures.
    storage = S3ObjectStorage(_settings(S3_PUBLIC_BASE_URL="https://cdn.example.com"))
    url = storage.sign_render_url("u1/items/i1/cutout.png")
    assert url.startswith("http://minio:9000/aidrobe/u1/items/i1/cutout.png?")


def test_render_signing_falls_back_to_internal_endpoint_without_public_base() -> None:
    storage = S3ObjectStorage(_settings())
    url = storage.sign_render_url("u1/items/i1/cutout.png")
    assert url.startswith("http://minio:9000/aidrobe/u1/items/i1/cutout.png?")
