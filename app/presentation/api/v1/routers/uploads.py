from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.bootstrap.container import object_storage, sts_provider
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.user import User
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.common import ResponseModel
from app.presentation.api.v1.schemas.uploads import PresignUploadRequest, PresignUploadResponse

router = APIRouter(prefix="/v1/uploads", tags=["uploads"])

ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
# Phase E5 — bucket aliases the iOS client may request. The aliasing layer
# means we can rename the actual OSS bucket without breaking clients.
_BUCKET_ALIAS = {
    "items": "ALIYUN_OSS_BUCKET",  # default bucket
    "ootd": "ALIYUN_OSS_BUCKET",
    "private": "ALIYUN_OSS_BUCKET",
    "derived": "ALIYUN_OSS_BUCKET",
}


@router.post("/presign", response_model=ResponseModel[PresignUploadResponse])
async def create_presigned_upload(
    payload: PresignUploadRequest,
) -> ResponseModel[PresignUploadResponse]:
    if payload.content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
        raise FriendlyException(
            error_codes.VALIDATION_ERROR,
            "Only image uploads are supported",
            status_code=422,
        )

    result = await object_storage().create_presigned_upload(
        file_name=payload.file_name,
        content_type=payload.content_type,
        max_bytes=payload.max_content_length,
    )
    return ResponseModel(data=PresignUploadResponse(**result.__dict__))


class StsTokenRequest(BaseModel):
    bucket_alias: Literal["items", "ootd", "private", "derived"] = "items"
    action: Literal["upload", "read"] = "upload"


class StsTokenResponse(BaseModel):
    access_key_id: str
    access_key_secret: str
    security_token: str
    expiration: str
    bucket: str
    region: str
    endpoint: str
    object_key_prefix: str


@router.post("/sts-token", response_model=ResponseModel[StsTokenResponse])
async def issue_sts_token(
    payload: StsTokenRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ResponseModel[StsTokenResponse]:
    """Issue narrow-scoped temporary OSS credentials.

    The returned token can ONLY (a) hit `users/<this-user>/<action>/` prefix
    of the named bucket, (b) for the requested verb, (c) for the configured
    TTL. iOS uses this to upload originals directly without ever holding a
    permanent AccessKey — server never sees the bytes (saves bandwidth)."""
    if not settings.ENABLE_OSS_STS:
        # Kill switch: when off, clients must fall back to /presign which
        # signs with the backend's own credentials per object.
        raise FriendlyException(
            error_codes.FORBIDDEN,
            "STS not enabled",
            status_code=503,
        )

    bucket_field = _BUCKET_ALIAS.get(payload.bucket_alias)
    if bucket_field is None:
        raise FriendlyException(
            error_codes.VALIDATION_ERROR,
            f"unknown bucket alias: {payload.bucket_alias}",
            status_code=422,
        )
    bucket = getattr(settings, bucket_field, "")
    if not bucket:
        # Operator hasn't configured this bucket yet — fail closed rather
        # than issuing creds for an empty target.
        raise FriendlyException(
            error_codes.FORBIDDEN,
            "bucket not configured",
            status_code=503,
        )

    creds = await sts_provider().issue(
        user_id=current_user.id,
        bucket=bucket,
        action=payload.action,
        ttl_seconds=settings.ALIYUN_STS_TTL_SECONDS,
    )
    return ResponseModel(
        data=StsTokenResponse(
            access_key_id=creds.access_key_id,
            access_key_secret=creds.access_key_secret,
            security_token=creds.security_token,
            expiration=creds.expiration,
            bucket=creds.bucket,
            region=creds.region,
            endpoint=creds.endpoint,
            object_key_prefix=creds.object_key_prefix,
        )
    )
