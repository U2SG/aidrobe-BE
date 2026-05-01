from fastapi import APIRouter

from app.bootstrap.container import object_storage
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.presentation.api.v1.schemas.common import ResponseModel
from app.presentation.api.v1.schemas.uploads import PresignUploadRequest, PresignUploadResponse

router = APIRouter(prefix="/v1/uploads", tags=["uploads"])

ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


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
