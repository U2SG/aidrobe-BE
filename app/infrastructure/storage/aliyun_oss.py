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

from app.application.ports.storage import PresignResult
from app.config.settings import Settings


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
    ) -> PresignResult:
        """Server-signed PUT URL fallback when the client cannot hold STS.

        Prefer STS for client uploads — this path exists for trusted server
        flows (e.g. import worker writing into the user's bucket prefix on
        behalf of the user)."""
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", file_name).strip("._") or "upload"
        object_key = f"wardrobe/{uuid.uuid4()}/{safe_name}"
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

    async def put_object(self, key: str, data: bytes, content_type: str) -> str:
        # `bucket.put_object` is sync; this method is async for port-parity
        # with S3ObjectStorage. Wrap with `asyncio.to_thread` only when the
        # SDK becomes the throughput bottleneck — for now the single-call
        # latency is dominated by network, not the GIL.
        self._bucket.put_object(  # type: ignore[attr-defined]
            key, data, headers={"Content-Type": content_type}
        )
        return self._public_url(key)

    def _public_url(self, key: str) -> str:
        if self.settings.ALIYUN_OSS_PUBLIC_BASE_URL:
            base = self.settings.ALIYUN_OSS_PUBLIC_BASE_URL.rstrip("/")
            return f"{base}/{key}"
        # Fallback: synthesize from endpoint + bucket. NOTE this is the OSS
        # raw URL — for production reads route through CDN via cdn_url.py.
        return f"{self.settings.ALIYUN_OSS_ENDPOINT}/{self.settings.ALIYUN_OSS_BUCKET}/{key}"
