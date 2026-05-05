import io
import uuid
from typing import TYPE_CHECKING

import httpx
import structlog

from app.application.ports.image_processor import ImageProcessingResult
from app.config.settings import Settings

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)

try:
    from PIL import Image, ImageOps
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

try:
    import rembg
    _REMBG_AVAILABLE = True
except ImportError:
    _REMBG_AVAILABLE = False


def _compute_quality(image: "Image.Image") -> tuple[float, list[str]]:
    """Return (quality_score 0-1, quality_flags)."""
    import numpy as np
    from PIL import ImageFilter

    flags: list[str] = []
    gray = image.convert("L")
    arr = np.array(gray, dtype=float)

    # blur: Laplacian variance
    blur_score = float(np.array(gray.filter(ImageFilter.FIND_EDGES)).var())
    if blur_score < 100:
        flags.append("blur")

    # brightness
    brightness = float(arr.mean())
    if brightness < 40:
        flags.append("low_light")
    elif brightness > 220:
        flags.append("overexposed")

    # subject area ratio (non-white pixels)
    non_white = float((arr < 240).sum()) / arr.size
    if non_white < 0.1:
        flags.append("subject_too_small")

    quality = min(1.0, max(0.0, (
        0.4 * min(blur_score / 500, 1.0)
        + 0.3 * (1.0 - abs(brightness - 128) / 128)
        + 0.3 * min(non_white / 0.3, 1.0)
    )))
    return quality, flags


class RembgImageProcessor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def process(
        self, image_url: str, item_id: str, user_id: str
    ) -> ImageProcessingResult:
        if not _PIL_AVAILABLE:
            # no PIL — return original URL for all assets
            return ImageProcessingResult(
                normalized_url=image_url,
                thumbnail_url=image_url,
                cutout_url=image_url,
                quality_score=0.5,
                quality_flags=["pil_unavailable"],
            )

        from PIL import Image, ImageOps

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(image_url)
            resp.raise_for_status()
            raw_bytes = resp.content

        img = Image.open(io.BytesIO(raw_bytes))
        img = ImageOps.exif_transpose(img)

        # convert to RGB for JPEG output
        rgb = img.convert("RGB")

        # compress to < 500 KB
        normalized_buf = io.BytesIO()
        quality = 85
        rgb.save(normalized_buf, format="JPEG", quality=quality, optimize=True)
        while normalized_buf.tell() > 500 * 1024 and quality > 40:
            quality -= 10
            normalized_buf = io.BytesIO()
            rgb.save(normalized_buf, format="JPEG", quality=quality, optimize=True)
        normalized_bytes = normalized_buf.getvalue()

        # thumbnail 256x256
        thumb = rgb.copy()
        thumb.thumbnail((256, 256))
        thumb_buf = io.BytesIO()
        thumb.save(thumb_buf, format="JPEG", quality=75)
        thumb_bytes = thumb_buf.getvalue()

        # background removal
        if _REMBG_AVAILABLE:
            try:
                cutout_bytes = rembg.remove(raw_bytes)
            except Exception as exc:
                logger.warning("rembg_failed", error=str(exc))
                cutout_bytes = normalized_bytes
                cutout_ext = "jpg"
            else:
                cutout_ext = "png"
        else:
            cutout_bytes = normalized_bytes
            cutout_ext = "jpg"

        # quality score
        try:
            quality_score, quality_flags = _compute_quality(rgb)
        except Exception:
            quality_score, quality_flags = 0.5, []

        # upload to S3
        normalized_url = await self._upload(normalized_bytes, f"{user_id}/{item_id}/normalized.jpg", "image/jpeg")
        thumbnail_url = await self._upload(thumb_bytes, f"{user_id}/{item_id}/thumbnail.jpg", "image/jpeg")
        cutout_mime = "image/png" if cutout_ext == "png" else "image/jpeg"
        cutout_url = await self._upload(cutout_bytes, f"{user_id}/{item_id}/cutout.{cutout_ext}", cutout_mime)

        return ImageProcessingResult(
            normalized_url=normalized_url,
            thumbnail_url=thumbnail_url,
            cutout_url=cutout_url,
            quality_score=quality_score,
            quality_flags=quality_flags,
        )

    async def _upload(self, data: bytes, key: str, content_type: str) -> str:
        import boto3
        s = self.settings
        client = boto3.client(
            "s3",
            endpoint_url=s.S3_ENDPOINT_URL,
            region_name=s.S3_REGION,
            aws_access_key_id=s.S3_ACCESS_KEY_ID,
            aws_secret_access_key=s.S3_SECRET_ACCESS_KEY,
        )
        client.put_object(
            Bucket=s.S3_BUCKET,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        if s.S3_PUBLIC_BASE_URL:
            return f"{s.S3_PUBLIC_BASE_URL.rstrip('/')}/{key}"
        if s.S3_ENDPOINT_URL:
            return f"{s.S3_ENDPOINT_URL.rstrip('/')}/{s.S3_BUCKET}/{key}"
        return f"https://{s.S3_BUCKET}.s3.{s.S3_REGION}.amazonaws.com/{key}"
