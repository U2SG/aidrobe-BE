from app.application.ports.ocr import OcrResult
from app.infrastructure.importers.adapters.receipt import build_candidate_payload


class OrderScreenshotAdapter:
    """Maps an OCR result from an *order* screenshot (e.g. Taobao / JD order
    detail) or a forwarded *shopping email* into a candidate payload.

    Shares the receipt mapping; kept distinct so per-source tweaks stay
    isolated from the receipt adapter.
    """

    def to_candidate_payload(self, ocr: OcrResult, image_url: str) -> dict[str, object]:
        return build_candidate_payload(ocr, image_url)
