import re
import uuid

import boto3

from app.application.ports.storage import PresignResult
from app.config.settings import Settings


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

    async def create_presigned_upload(
        self,
        file_name: str,
        content_type: str,
        max_bytes: int,
    ) -> PresignResult:
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", file_name).strip("._") or "upload"
        object_key = f"wardrobe/{uuid.uuid4()}/{safe_name}"
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

    def _file_url(self, object_key: str) -> str:
        if self.settings.S3_PUBLIC_BASE_URL:
            return f"{self.settings.S3_PUBLIC_BASE_URL.rstrip('/')}/{object_key}"
        if self.settings.S3_ENDPOINT_URL:
            return f"{self.settings.S3_ENDPOINT_URL.rstrip('/')}/{self.bucket}/{object_key}"
        return f"https://{self.bucket}.s3.{self.settings.S3_REGION}.amazonaws.com/{object_key}"
