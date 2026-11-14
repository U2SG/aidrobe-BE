import io
import uuid

import httpx
import pytest
from PIL import Image

from app.config.settings import Settings
from app.infrastructure.importers import external_http
from app.infrastructure.storage.object_key import trusted_item_image_key
from app.infrastructure.storage.source_image import (
    InvalidSourceImage,
    download_external_source_image,
    load_source_bytes,
    resolve_import_source_key,
)

USER_ID = uuid.UUID("10000000-0000-4000-8000-000000000001")


def _s3() -> Settings:
    return Settings(
        STORAGE_PROVIDER="s3",
        S3_BUCKET="aidrobe",
        S3_ENDPOINT_URL="http://minio:9000",
        S3_PUBLIC_BASE_URL="https://images.example/aidrobe",
    )


@pytest.mark.parametrize(
    ("settings", "base"),
    [
        (Settings(STORAGE_PROVIDER="local"), "http://localhost:9000/local/files"),
        (_s3(), "https://images.example/aidrobe"),
        (_s3(), "http://minio:9000/aidrobe"),
        (_s3(), "https://aidrobe.s3.amazonaws.com"),
        (_s3(), "https://s3.amazonaws.com/aidrobe"),
        (_s3(), "https://aidrobe.s3.us-east-1.amazonaws.com"),
        (
            Settings(
                STORAGE_PROVIDER="aliyun_oss",
                ALIYUN_OSS_ENDPOINT="oss-cn-hangzhou.aliyuncs.com",
                ALIYUN_OSS_BUCKET="aidrobe-items",
            ),
            "https://aidrobe-items.oss-cn-hangzhou.aliyuncs.com",
        ),
    ],
)
def test_storage_upload_references_survive_expired_signatures(settings: Settings, base: str) -> None:
    url = f"{base}/users/{USER_ID}/buy%20me.jpg?Expires=1&Signature=expired"
    assert resolve_import_source_key(url, USER_ID, settings) == f"users/{USER_ID}/buy me.jpg"


@pytest.mark.parametrize(
    "url",
    [
        "https://images.example/aidrobe/users/00000000-0000-4000-8000-000000000002/x.jpg",
        f"https://images.example/wrong-bucket/users/{USER_ID}/x.jpg",
        f"https://evil.example/aidrobe/users/{USER_ID}/x.jpg",
        f"https://aidrobe.evil.example/users/{USER_ID}/x.jpg",
        f"https://images.example/aidrobe/users/{USER_ID}/%2e%2e/x.jpg",
        f"https://images.example/aidrobe/{USER_ID}/items/a/cutout.png",
        f"http://minio:9000/other/users/{USER_ID}/x.jpg",
    ],
)
async def test_unsupported_reference_never_reaches_storage_or_http(monkeypatch, url: str) -> None:
    def unexpected_read(*args, **kwargs):
        pytest.fail("The unverified reference reached a storage or HTTP client")

    monkeypatch.setattr("app.config.settings.get_settings", _s3)
    monkeypatch.setattr("app.bootstrap.container.object_storage", unexpected_read)
    monkeypatch.setattr(httpx, "AsyncClient", unexpected_read)
    with pytest.raises(InvalidSourceImage, match="unavailable"):
        await load_source_bytes(url, None, user_id=USER_ID)


def test_unsupported_purpose_bucket_is_not_external() -> None:
    settings = Settings(
        STORAGE_PROVIDER="aliyun_oss",
        ALIYUN_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        ALIYUN_OSS_BUCKET="aidrobe-items",
        ALIYUN_OSS_BUCKET_OOTD="aidrobe-ootd",
    )
    url = f"https://aidrobe-ootd.oss-cn-hangzhou.aliyuncs.com/users/{USER_ID}/x.jpg"
    with pytest.raises(InvalidSourceImage, match="unavailable"):
        resolve_import_source_key(url, USER_ID, settings)


def test_legacy_generated_key_only_resolves_for_an_owned_item() -> None:
    key = f"{USER_ID}/items/a/cutout.png"
    url = f"https://images.example/aidrobe/{key}"
    assert trusted_item_image_key(url, USER_ID, _s3()) == key
    assert trusted_item_image_key(url, uuid.uuid4(), _s3()) is None
    with pytest.raises(InvalidSourceImage):
        resolve_import_source_key(url, USER_ID, _s3())


@pytest.mark.parametrize(
    "url",
    [
        None,
        f"https://images.example/other/users/{USER_ID}/x.jpg",
        f"https://images.example/aidrobe/users/{USER_ID}/different.jpg",
        f"https://shop.example/asset/users/{USER_ID}/x.jpg",
        f"http://localhost:9000/local/files/users/{USER_ID}/x.jpg",
    ],
)
def test_saved_reference_must_still_name_the_active_bucket_and_exact_key(url: str | None) -> None:
    with pytest.raises(InvalidSourceImage, match="unavailable"):
        resolve_import_source_key(url, USER_ID, _s3(), expected_key=f"users/{USER_ID}/x.jpg")


async def test_put_object_urls_round_trip_exact_keys_without_provider_network(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from app.infrastructure.storage.aliyun_oss import AliyunOssStorage
    from app.infrastructure.storage.local_storage import LocalObjectStorage
    from app.infrastructure.storage.s3_storage import S3ObjectStorage

    def no_network(*args, **kwargs) -> None:
        pass

    client = SimpleNamespace(put_object=no_network)
    monkeypatch.setattr("app.infrastructure.storage.s3_storage.boto3.client", lambda *args, **kwargs: client)
    monkeypatch.setattr(AliyunOssStorage, "_build_bucket", lambda _: client)
    local_cfg = Settings(STORAGE_PROVIDER="local")
    s3_cfg = Settings(STORAGE_PROVIDER="s3", S3_BUCKET="aidrobe")
    oss_cfg = Settings(
        STORAGE_PROVIDER="aliyun_oss",
        ALIYUN_OSS_BUCKET="aidrobe",
        ALIYUN_OSS_ENDPOINT="oss-cn-hangzhou.aliyuncs.com",
        ALIYUN_OSS_ACCESS_KEY_ID="stub",
        ALIYUN_OSS_ACCESS_KEY_SECRET="stub",
    )
    stores = (
        (local_cfg, LocalObjectStorage(storage_root=str(tmp_path))),
        (s3_cfg, S3ObjectStorage(s3_cfg)),
        (oss_cfg, AliyunOssStorage(oss_cfg)),
    )
    key = f"users/{USER_ID}/wardrobe/imports/photo.jpg"
    for cfg, storage in stores:
        url = await storage.put_object(key, b"snapshot", "image/jpeg")
        assert resolve_import_source_key(url, USER_ID, cfg, expected_key=key) == key


@pytest.fixture
def external_transport(monkeypatch):
    async_client = httpx.AsyncClient

    async def public_ip(host: str, port: int) -> str:
        assert port == 443
        return "203.0.114.2"

    monkeypatch.setattr(external_http, "_resolve_public_address", public_ip)

    def install(handler) -> None:
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kwargs: async_client(transport=httpx.MockTransport(handler), **kwargs)
        )

    return install


async def test_external_image_redirect_and_content_are_checked(external_transport) -> None:
    encoded = io.BytesIO()
    Image.new("RGB", (4, 4)).save(encoded, format="PNG")
    requests: list[tuple[str, str | None, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.headers["host"], request.extensions.get("sni_hostname"), request.url.path))
        assert request.url.host == "203.0.114.2"
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "https://cdn.example/photo"})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=encoded.getvalue())

    external_transport(handler)
    data, media_type = await download_external_source_image("https://shop.example/redirect", _s3())
    assert (data, media_type) == (encoded.getvalue(), "image/png")
    assert requests == [("shop.example", "shop.example", "/redirect"), ("cdn.example", "cdn.example", "/photo")]


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(302, headers={"location": "https://images.example/aidrobe/users/x/img.jpg"}), "Internal"),
        (httpx.Response(200, headers={"content-type": "text/html"}), "content type"),
        (httpx.Response(200, headers={"content-type": "image/png"}, content=b"bad"), "contents"),
        (httpx.Response(200, headers={"content-type": "image/png", "content-length": "10485761"}), "too large"),
    ],
)
async def test_unsafe_external_image_is_a_permanent_failure(external_transport, response, reason) -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.headers["host"])
        return response

    external_transport(handler)
    with pytest.raises(InvalidSourceImage, match=reason):
        await download_external_source_image("https://shop.example/photo", _s3())
    assert requests == ["shop.example"]
