from typing import Protocol

from app.application.ports.ocr import OcrResult
from app.common.enums import InputType
from app.infrastructure.importers.adapters.order_screenshot import OrderScreenshotAdapter
from app.infrastructure.importers.adapters.receipt import ReceiptAdapter


class OcrCandidateAdapter(Protocol):
    def to_candidate_payload(self, ocr: OcrResult, image_url: str) -> dict[str, object]: ...


# input_type values that flow through the OCR pipeline rather than the
# vision / link-parse pipelines. Mirrors DOMAIN_ADAPTER_MAP in router.py.
OCR_ADAPTER_MAP: dict[str, OcrCandidateAdapter] = {
    InputType.RECEIPT_SCREENSHOT.value: ReceiptAdapter(),
    InputType.ORDER_SCREENSHOT.value: OrderScreenshotAdapter(),
    InputType.SHOPPING_EMAIL.value: OrderScreenshotAdapter(),
}


def ocr_adapter_for(input_type: str) -> OcrCandidateAdapter | None:
    return OCR_ADAPTER_MAP.get(input_type)
