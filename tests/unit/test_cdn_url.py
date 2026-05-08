"""Phase E5 — CDN URL builder + image-processing param helper."""

from __future__ import annotations

from app.infrastructure.storage.cdn_url import (
    PREVIEW_PROCESS,
    THUMBNAIL_PROCESS,
    CdnConfig,
    build_oss_process_url,
    sign_cdn_url_type_a,
)


def test_build_oss_process_url_appends_query() -> None:
    url = "https://oss/bucket/key.jpg"
    assert build_oss_process_url(url, THUMBNAIL_PROCESS) == (
        "https://oss/bucket/key.jpg?x-oss-process=image/resize,m_fill,w_400,h_400/quality,q_75/format,jpg"
    )


def test_build_oss_process_url_with_existing_query_uses_amp() -> None:
    url = "https://oss/bucket/key.jpg?v=1"
    out = build_oss_process_url(url, THUMBNAIL_PROCESS)
    assert "?v=1&x-oss-process=" in out


def test_build_oss_process_url_passes_through_when_no_process() -> None:
    url = "https://oss/bucket/key.jpg"
    assert build_oss_process_url(url, None) == url


def test_sign_cdn_url_type_a_includes_auth_key() -> None:
    config = CdnConfig(base_url="https://cdn.example", auth_key="secret-32b-key")
    out = sign_cdn_url_type_a(
        config, "users/u1/items/abc.jpg", ttl_seconds=600, rand="deadbeef"
    )
    assert out.startswith("https://cdn.example/users/u1/items/abc.jpg?")
    assert "auth_key=" in out
    assert "deadbeef" in out


def test_sign_cdn_url_includes_process_query() -> None:
    """When the URL carries `?x-oss-process=...`, the signature must cover
    it so a client can't swap to a different (more expensive) variant."""
    config = CdnConfig(base_url="https://cdn.example", auth_key="secret-32b-key")
    out = sign_cdn_url_type_a(
        config,
        "users/u1/items/abc.jpg",
        process=PREVIEW_PROCESS,
        ttl_seconds=600,
        rand="deadbeef",
    )
    assert "x-oss-process=" in out
    assert "auth_key=" in out


def test_sign_cdn_url_changes_with_ttl() -> None:
    """Signature is bound to the expiration timestamp — different TTL ⇒
    different signature, so a client can't keep replaying an old URL."""
    config = CdnConfig(base_url="https://cdn.example", auth_key="secret-32b-key")
    a = sign_cdn_url_type_a(config, "k.jpg", ttl_seconds=600, rand="r")
    b = sign_cdn_url_type_a(config, "k.jpg", ttl_seconds=1200, rand="r")
    # auth_key segments include `<expires>-r-0-<sig>`; the expires value
    # differs even with the same nonce.
    assert a != b
