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
