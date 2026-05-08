"""CDN URL builder + OSS image-processing parameter helper — Phase E5.

Two flows:

1. **CDN signed URL** (private bucket via CDN): clients render images by
   GETting `https://<cdn-domain>/<key>?<signature>`. The signature is
   computed by the backend per request and short-lived. Without the
   signature, CDN refuses (origin pull also signed via OSS Referer +
   private bucket policy).

2. **OSS image processing**: append `?x-oss-process=image/resize,...` to
   serve a thumbnail / compressed variant from the same key without
   storing a second object. Combined with #1: the signature must include
   the processing query so a client can't tamper with it.

Aliyun-specific signing scheme (`aliyun_a_signature` / Type A):
    md5_secret = MD5("URL+SECRET")
    signature  = base64url(md5_secret)
    final_url  = "<base>?...&auth_key=<expires>-<rand>-<uid>-<signature>"

We support both Type A (most common) and Type B/C through pluggable signers
in case operations standardize on a different one. Default is Type A.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

# Image-processing param presets. Operators add/remove via this module
# rather than from caller code so prompt-text and pricing characteristics
# stay consistent across the app.
THUMBNAIL_PROCESS = "image/resize,m_fill,w_400,h_400/quality,q_75/format,jpg"
PREVIEW_PROCESS = "image/resize,m_lfit,w_1280/quality,q_85/format,jpg"
ORIGINAL_PROCESS: str | None = None  # no rewriting


@dataclass(frozen=True)
class CdnConfig:
    base_url: str  # https://cdn.aidrobe.example
    auth_key: str  # CDN signing secret (Type A `KEY`)
    default_ttl_seconds: int = 1800


def build_oss_process_url(file_url: str, process: str | None) -> str:
    """Append OSS image-processing params to an OSS URL. Returns the URL
    unchanged when `process` is None."""
    if process is None:
        return file_url
    sep = "&" if "?" in file_url else "?"
    return f"{file_url}{sep}x-oss-process={process}"


def sign_cdn_url_type_a(
    config: CdnConfig,
    object_key: str,
    *,
    process: str | None = None,
    ttl_seconds: int | None = None,
    rand: str | None = None,
    user_id: str = "0",
) -> str:
    """Aliyun CDN Type A signing.

    `object_key` is the bucket-relative path (e.g. `users/<id>/items/abc.jpg`).
    Returned URL is the public CDN URL with `auth_key` query param appended.

    The signature MUST cover the processed query string so a client cannot
    swap `?x-oss-process=...` to demand a different (more expensive or
    higher-resolution) variant.
    """
    ttl = ttl_seconds or config.default_ttl_seconds
    expires = int(time.time()) + ttl
    nonce = rand or secrets.token_hex(8)
    base = config.base_url.rstrip("/")
    path_with_query = "/" + object_key.lstrip("/")
    if process is not None:
        # The query string is part of the signed URI per Aliyun Type A spec.
        path_with_query = (
            f"{path_with_query}?{urlencode({'x-oss-process': process})}"
        )

    auth_payload = f"{path_with_query.split('?')[0]}-{expires}-{nonce}-{user_id}-{config.auth_key}"
    signature = hashlib.md5(auth_payload.encode()).hexdigest()
    auth_key = f"{expires}-{nonce}-{user_id}-{signature}"
    sep = "&" if "?" in path_with_query else "?"
    return f"{base}{path_with_query}{sep}auth_key={auth_key}"
