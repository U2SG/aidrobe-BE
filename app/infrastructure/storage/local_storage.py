import re
import uuid

from app.application.ports.storage import PresignResult


class LocalObjectStorage:
    def __init__(self, base_url: str = "http://localhost:9000/local") -> None:
        self.base_url = base_url.rstrip("/")

    async def create_presigned_upload(
        self,
        file_name: str,
        content_type: str,
        max_bytes: int,
    ) -> PresignResult:
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", file_name).strip("._") or "upload"
        object_key = f"wardrobe/{uuid.uuid4()}/{safe_name}"
        return PresignResult(
            upload_url=f"{self.base_url}/upload/{object_key}?content_type={content_type}&max_bytes={max_bytes}",
            file_url=f"{self.base_url}/files/{object_key}",
            object_key=object_key,
        )
