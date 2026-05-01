from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PresignResult:
    upload_url: str
    file_url: str
    object_key: str


class ObjectStoragePort(Protocol):
    async def create_presigned_upload(
        self,
        file_name: str,
        content_type: str,
        max_bytes: int,
    ) -> PresignResult: ...
