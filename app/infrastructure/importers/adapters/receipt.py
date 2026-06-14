from app.application.ports.ocr import OcrResult


def build_candidate_payload(ocr: OcrResult, image_url: str) -> dict[str, object]:
    """Map an OCR result into the candidate payload consumed by the existing
    import confirm flow (``CandidateItem`` shape + auto-fill extras)."""
    return {
        "title": ocr.title,
        # category/color_main are required by CandidateItem; fall back to a
        # neutral "other"/"unknown" so the user can correct on the confirm page.
        "category": ocr.category or "other",
        "color_main": ocr.color_main or "unknown",
        "size_label": ocr.size_label,
        "purchase_price": ocr.purchase_price,
        "purchase_date": ocr.purchase_date,
        "confidence_score": ocr.confidence_score,
        "original_image_url": image_url,
    }


class ReceiptAdapter:
    """Maps an OCR result from a purchase *receipt* screenshot into a candidate
    payload. The adapter exists per-input-type so source-specific
    post-processing can diverge later without forking the worker."""

    def to_candidate_payload(self, ocr: OcrResult, image_url: str) -> dict[str, object]:
        return build_candidate_payload(ocr, image_url)
