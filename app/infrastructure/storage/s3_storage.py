import re
import uuid
from collections.abc import Sequence
from urllib.parse import urlparse

import boto3
import structlog
from botocore.config import Config

from app.application.ports.storage import PresignResult
from app.config.settings import Settings

logger = structlog.get_logger(__name__)


def _public_presign_target(
    public_base_url: str | None, bucket: str
) -> tuple[str, str] | None:
    """Map `S3_PUBLIC_BASE_URL` (a *file* URL prefix) onto a boto3 signing
    target: (endpoint_url, addressing_style).

    - Path-style base (`http://host:9000/<bucket>`): endpoint is the bare
      host; addressing must be pinned to "path" so signed paths carry the
      bucket exactly once.
    - Virtual-host base (`https://<bucket>.host`): endpoint drops the bucket
      label and addressing is pinned to "virtual" — passing the full base as
      the endpoint would let botocore prepend the bucket AGAIN
      (`bucket.bucket.host`, PR #109 review).
    - Anything else (e.g. an opaque CDN host that hides the bucket entirely)
      can't be expressed as an S3 signing client — return None so the caller
      falls back to the internal client instead of emitting broken URLs."""
    if not public_base_url:
        return None
    parsed = urlparse(public_base_url.rstrip("/"))
    if not parsed.scheme or not parsed.netloc:
        return None
    path = parsed.path.rstrip("/")
    if path == f"/{bucket}":
        return f"{parsed.scheme}://{parsed.netloc}", "path"
    if not path and parsed.netloc.startswith(f"{bucket}."):
        return f"{parsed.scheme}://{parsed.netloc[len(bucket) + 1:]}", "virtual"
    return None


class S3ObjectStorage:
    def __init__(self, settings: Settings) -> None:
        if not settings.S3_BUCKET:
            raise ValueError("S3_BUCKET is required when STORAGE_PROVIDER=s3")
        self.settings = settings
        self.bucket = settings.S3_BUCKET
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL,
            region_name=settings.S3_REGION,
            aws_access_key_id=settings.S3_ACCESS_KEY_ID,
            aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
        )
        # Read-path presigns must be generated AGAINST the device-reachable
        # host: the GET signature binds the host, so a URL signed against the
        # in-cluster endpoint (minio:9000) 403s after any host rewrite —
        # verified empirically. Upload presigns stay on the internal client
        # (presigned POST signs the form policy, not the host; the FE rewrite
        # owns that path — see aidrobe-frontend CLAUDE.md).
        target = _public_presign_target(settings.S3_PUBLIC_BASE_URL, self.bucket)
        if target:
            endpoint, addressing_style = target
            self._render_sign_client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                region_name=settings.S3_REGION,
                aws_access_key_id=settings.S3_ACCESS_KEY_ID,
                aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
                config=Config(s3={"addressing_style": addressing_style}),
            )
        else:
            if settings.S3_PUBLIC_BASE_URL:
                logger.warning(
                    "s3.render_sign.public_base_unmappable",
                    public_base_url=settings.S3_PUBLIC_BASE_URL,
                )
            self._render_sign_client = self.client

    async def create_presigned_upload(
        self,
        file_name: str,
        content_type: str,
        max_bytes: int,
        *,
        key_prefix: str = "",
    ) -> PresignResult:
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", file_name).strip("._") or "upload"
        prefix = key_prefix.strip("/") + "/" if key_prefix else ""
        object_key = f"{prefix}wardrobe/{uuid.uuid4()}/{safe_name}"
        presigned = self.client.generate_presigned_post(
            self.bucket,
            object_key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 1, max_bytes],
            ],
            ExpiresIn=self.settings.S3_PRESIGN_EXPIRES_SECONDS,
        )
        file_url = self._file_url(object_key)
        return PresignResult(
            upload_url=presigned["url"],
            file_url=file_url,
            object_key=object_key,
            fields=presigned["fields"],
        )

    async def delete_objects(self, keys: Sequence[str]) -> None:
        if not keys:
            return
        for i in range(0, len(keys), 1000):
            batch = list(keys[i : i + 1000])
            resp = self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": False},
            )
            errors = resp.get("Errors", [])
            if errors:
                logger.error(
                    "s3.delete_objects.errors",
                    errors=errors,
                    bucket=self.bucket,
                )
                raise RuntimeError(
                    f"S3 delete_objects returned {len(errors)} error(s): "
                    f"{errors[0].get('Code')}: {errors[0].get('Message')}"
                )

    async def delete_prefix(self, prefix: str) -> int:
        if not prefix.strip("/"):
            raise ValueError("delete_prefix requires a non-root prefix")
        deleted = 0
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys = [obj["Key"] for obj in page.get("Contents", [])]
            if keys:
                await self.delete_objects(keys)
                deleted += len(keys)
        return deleted

    async def put_object(self, key: str, data: bytes, content_type: str) -> str:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return self._file_url(key)

    async def get_object(self, key: str) -> bytes:
        resp = self.client.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"].read()  # type: ignore[no-any-return]

    def sign_render_url(
        self,
        key: str,
        *,
        ttl_seconds: int = 1800,
        process: str | None = None,
    ) -> str:
        """S3 presigned GET against the public endpoint (host-bound signature).
        `process` is OSS-specific; on S3 it is ignored (no equivalent
        server-side image processing)."""
        return self._render_sign_client.generate_presigned_url(  # type: ignore[no-any-return]
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )

    def _file_url(self, object_key: str) -> str:
        if self.settings.S3_PUBLIC_BASE_URL:
            return f"{self.settings.S3_PUBLIC_BASE_URL.rstrip('/')}/{object_key}"
        if self.settings.S3_ENDPOINT_URL:
            return f"{self.settings.S3_ENDPOINT_URL.rstrip('/')}/{self.bucket}/{object_key}"
        return f"https://{self.bucket}.s3.{self.settings.S3_REGION}.amazonaws.com/{object_key}"
