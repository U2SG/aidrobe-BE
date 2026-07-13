"""Aliyun OSS adapter — Phase E5.

Implements `ObjectStoragePort` against `oss2`. Distinct from S3 even though
OSS is "S3-compatible" because:
- Image processing uses `?x-oss-process=...` (OSS-only); S3 has no equivalent.
- Signed URLs use a different scheme; mixing the two leads to subtle errors.
- STS / RAM role semantics differ enough that the credential-management
  surface area is different.

NOT covered by automated tests in this repo — needs a real OSS bucket and
network egress. Tested live during deployment.

The `oss2` SDK is imported lazily so dev environments without it still load
the module.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from typing import cast

import structlog

from app.application.ports.storage import PresignResult
from app.config.settings import Settings

logger = structlog.get_logger(__name__)


class AliyunOssStorage:
    def __init__(self, settings: Settings) -> None:
        for field in (
            "ALIYUN_OSS_ACCESS_KEY_ID",
            "ALIYUN_OSS_ACCESS_KEY_SECRET",
            "ALIYUN_OSS_ENDPOINT",
            "ALIYUN_OSS_BUCKET",
        ):
            if not getattr(settings, field, None):
                raise ValueError(
                    f"AliyunOssStorage requires {field} to be set"
                )
        self.settings = settings
        self._bucket = self._build_bucket()

    def _build_bucket(self) -> object:
        import oss2  # type: ignore[import-not-found]

        auth = oss2.Auth(
            self.settings.ALIYUN_OSS_ACCESS_KEY_ID,
            self.settings.ALIYUN_OSS_ACCESS_KEY_SECRET,
        )
        return oss2.Bucket(
            auth,
            self.settings.ALIYUN_OSS_ENDPOINT,
            self.settings.ALIYUN_OSS_BUCKET,
        )

    async def create_presigned_upload(
        self,
        file_name: str,
        content_type: str,
        max_bytes: int,
        *,
        key_prefix: str = "",
    ) -> PresignResult:
        """Server-signed PUT URL fallback when the client cannot hold STS.

        Prefer STS for client uploads — this path exists for trusted server
        flows (e.g. import worker writing into the user's bucket prefix on
        behalf of the user)."""
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", file_name).strip("._") or "upload"
        prefix = key_prefix.strip("/") + "/" if key_prefix else ""
        object_key = f"{prefix}wardrobe/{uuid.uuid4()}/{safe_name}"
        # `sign_url` returns a string URL; OSS signs the verb + headers in
        # the URL itself, so the client just PUTs the bytes to it.
        ttl = self.settings.S3_PRESIGN_EXPIRES_SECONDS  # reuse the existing knob
        upload_url = self._bucket.sign_url(  # type: ignore[attr-defined]
            "PUT",
            object_key,
            ttl,
            slash_safe=True,
            params={"Content-Type": content_type},
        )
        return PresignResult(
            upload_url=upload_url,
            file_url=self._public_url(object_key),
            object_key=object_key,
            fields={},
        )

    async def delete_objects(self, keys: Sequence[str]) -> None:
        if not keys:
            return
        for i in range(0, len(keys), 1000):
            batch = list(keys[i : i + 1000])
            result = self._bucket.batch_delete_objects(batch)  # type: ignore[attr-defined]
            # Missing keys are success (OSS batch delete is idempotent); a
            # non-200 means the request itself failed and must retry.
            if result.status != 200:
                logger.error(
                    "oss.delete_objects.error",
                    status=result.status,
                    keys_count=len(batch),
                )
                raise RuntimeError(
                    f"OSS batch_delete_objects returned status {result.status}"
                )

    async def delete_prefix(self, prefix: str) -> int:
        """Sweep `prefix` across the default bucket AND every configured
        per-purpose bucket (items/ootd/private/derived). STS-uploaded objects
        land in per-purpose buckets whose URLs `extract_internal_object_key`
        deliberately refuses to resolve — the prefix sweep is the only
        deletion path that reaches them."""
        if not prefix.strip("/"):
            raise ValueError("delete_prefix requires a non-root prefix")
        import oss2  # type: ignore[import-not-found]

        auth = oss2.Auth(
            self.settings.ALIYUN_OSS_ACCESS_KEY_ID,
            self.settings.ALIYUN_OSS_ACCESS_KEY_SECRET,
        )
        bucket_names = {self.settings.ALIYUN_OSS_BUCKET}
        for field in (
            "ALIYUN_OSS_BUCKET_ITEMS",
            "ALIYUN_OSS_BUCKET_OOTD",
            "ALIYUN_OSS_BUCKET_PRIVATE",
            "ALIYUN_OSS_BUCKET_DERIVED",
        ):
            name = getattr(self.settings, field, "")
            if name:
                bucket_names.add(name)

        deleted = 0
        for name in sorted(bucket_names):
            bucket = oss2.Bucket(auth, self.settings.ALIYUN_OSS_ENDPOINT, name)
            keys = [obj.key for obj in oss2.ObjectIterator(bucket, prefix=prefix)]
            for i in range(0, len(keys), 1000):
                batch = keys[i : i + 1000]
                result = bucket.batch_delete_objects(batch)
                if result.status != 200:
                    logger.error(
                        "oss.delete_prefix.error",
                        status=result.status,
                        bucket=name,
                        keys_count=len(batch),
                    )
                    raise RuntimeError(
                        f"OSS batch_delete_objects returned status {result.status}"
                    )
                deleted += len(batch)
        return deleted

    async def put_object(self, key: str, data: bytes, content_type: str) -> str:
        # `bucket.put_object` is sync; this method is async for port-parity
        # with S3ObjectStorage. Wrap with `asyncio.to_thread` only when the
        # SDK becomes the throughput bottleneck — for now the single-call
        # latency is dominated by network, not the GIL.
        self._bucket.put_object(  # type: ignore[attr-defined]
            key, data, headers={"Content-Type": content_type}
        )
        return self._public_url(key)

    async def get_object(self, key: str) -> bytes:
        # Sync SDK call; async for port-parity (see put_object note).
        return self._bucket.get_object(key).read()  # type: ignore[attr-defined,no-any-return]

    def _public_url(self, key: str) -> str:
        if self.settings.ALIYUN_OSS_PUBLIC_BASE_URL:
            base = self.settings.ALIYUN_OSS_PUBLIC_BASE_URL.rstrip("/")
            return f"{base}/{key}"
        # Fallback: synthesize from endpoint + bucket. NOTE this is the OSS
        # raw URL — for private buckets use `sign_get_url` instead (or
        # route through CDN via cdn_url.py once that is enabled).
        return f"{self.settings.ALIYUN_OSS_ENDPOINT}/{self.settings.ALIYUN_OSS_BUCKET}/{key}"

    def sign_render_url(
        self,
        key: str,
        *,
        ttl_seconds: int = 1800,
        process: str | None = None,
    ) -> str:
        """ObjectStoragePort interface name — delegates to sign_get_url."""
        return self.sign_get_url(key, ttl_seconds=ttl_seconds, process=process)

    def sign_get_url(
        self,
        key: str,
        *,
        ttl_seconds: int = 1800,
        process: str | None = None,
    ) -> str:
        """Return a short-lived signed GET URL for a private-bucket object.

        Use this when CDN isn't yet provisioned: the backend signs each
        render directly. Optionally bakes `?x-oss-process=...` into the
        signature so a client can't tamper with the resize/quality params.

        TTL note: 30 min default is long enough for one screen render +
        retries; clients re-fetch the URL on cache miss rather than
        holding a long-TTL token."""
        params: dict[str, str] = {}
        if process is not None:
            params["x-oss-process"] = process
        signed = self._bucket.sign_url(  # type: ignore[attr-defined]
            "GET",
            key,
            ttl_seconds,
            slash_safe=True,
            params=params,
        )
        return cast(str, signed)
