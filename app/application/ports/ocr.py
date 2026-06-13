from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OcrResult:
    """Structured fields extracted from a receipt / order screenshot / shopping email.

    All fields except ``title`` are optional because real-world receipts vary
    wildly in what they expose. ``purchase_price`` is kept as a string and
    ``purchase_date`` as an ISO-8601 date string so the value survives a JSONB
    round-trip in ``import_tasks.candidate_payload`` (JSONB can hold neither
    ``Decimal`` nor ``date``); the confirm use case coerces them to
    ``Decimal`` / ``date`` when materializing the wardrobe item.
    """

    title: str
    category: str | None = None
    color_main: str | None = None
    size_label: str | None = None
    purchase_price: str | None = None
    purchase_date: str | None = None
    confidence_score: float = 0.0


class OcrPort(Protocol):
    async def extract_text(self, image_url: str) -> OcrResult: ...
