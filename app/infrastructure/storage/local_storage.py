import re
import uuid
from pathlib import Path

from app.application.ports.storage import PresignResult


class LocalObjectStorage:
    def __init__(
        self,
        base_url: str = "http://localhost:9000/local",
        storage_root: str = "/tmp/aidrobe_local_storage",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.storage_root = Path(storage_root)

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

    async def put_object(self, key: str, data: bytes, content_type: str) -> str:
        dest = self.storage_root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return f"{self.base_url}/files/{key}"
