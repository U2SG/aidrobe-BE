from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class PresignResult:
    upload_url: str
    file_url: str
    object_key: str
    # Non-empty when the upload_url is a presigned POST (S3 multipart form).
    # Clients must include these fields alongside the file in the form body.
    fields: dict[str, str] = field(default_factory=dict)


class ObjectStoragePort(Protocol):
    async def create_presigned_upload(
        self,
        file_name: str,
        content_type: str,
        max_bytes: int,
    ) -> PresignResult: ...

    async def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str,
    ) -> str:
        """Upload bytes directly and return the public file URL."""
        ...

    async def get_object(self, key: str) -> bytes:
        """Read object bytes straight from storage by key.

        Used by internal pipelines (e.g. the cutout worker) that need the
        source image and already hold the object key — reading by key avoids
        an HTTP round-trip and the SSRF guard that (correctly) blocks our own
        private-network storage host."""
        ...

    def sign_render_url(
        self,
        key: str,
        *,
        ttl_seconds: int = 1800,
        process: str | None = None,
    ) -> str:
        """Phase E5 — return a short-lived URL the client can GET.

        For private buckets this signs the request; for public/local
        backends it just constructs the URL. `process` carries an optional
        OSS image-processing string (e.g. resize+quality); when supported
        by the backend the signature covers it so clients cannot tamper."""
        ...
