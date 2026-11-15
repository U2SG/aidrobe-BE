from app.application.ports.ocr import OcrResult


class FakeOcr:
    """Deterministic OCR stub so the receipt / order / email import flow is
    fully testable without a real OCR provider or API key.

    The returned values are keyed off substrings in the image URL so tests can
    steer the outcome (mirrors how ``FakeVision`` keys off ``fail`` / ``pants``).
    """

    async def extract_text(self, image_url: str, *, source_key: str | None = None) -> OcrResult:
        if "fail" in image_url:
            raise ValueError("fake ocr failure")
        if "pants" in image_url or "bottom" in image_url:
            return OcrResult(
                title="UNIQLO Slim Fit Chino",
                category="bottom",
                color_main="beige",
                size_label="M",
                purchase_price="199.00",
                purchase_date="2026-01-15",
                confidence_score=0.82,
            )
        return OcrResult(
            title="MUJI Oxford Shirt",
            category="top",
            color_main="white",
            size_label="L",
            purchase_price="299.00",
            purchase_date="2026-02-20",
            confidence_score=0.88,
        )
