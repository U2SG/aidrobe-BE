"""V3-004 — ingest a purchase candidate from a link, screenshot or order image.

One normalized contract for four source types, built entirely out of capability
that already exists in this service:

- `share_link` / `ask_link` — `RoutedLinkParser` (per-domain adapters + SSRF
  guard) extracts the product image and the page text, then `VisionPort` reads
  the image.
- `screenshot` — sign the stored object key, then `VisionPort`.
- `order_image` — sign the stored object key, then `OcrPort`: a receipt states
  category, color AND price as text, which vision alone cannot read.

`share_link` and `ask_link` are the same pipeline on purpose: the enum records
*where the user was* when they handed us the link (share sheet vs Ask), which
V3-002 analytics and V3-009 entry-point copy care about, and a link is a link.

**Parse failure never raises** (acceptance 2). A candidate row is always
persisted, `status` is `parse_failed` instead of `parsed`, and `parsed_item`
keeps whatever partial attributes were recovered so the client can show and
edit them. A hard error is reserved for a bad *request*: a source reference
that does not match `source_type`, an unsafe URL, or an object key that is not
this user's.

**Durable truth only.** A link is stored as-is; an upload is stored as its
stable `object_key`. Signed URLs are minted per call and never persisted
(V3_SPEC §13.5) — that is why the storage port is signed at ingest time and the
key, not the URL, goes in the row.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import structlog

from app.application.ports.ocr import OcrPort
from app.application.ports.vision import VisionPort
from app.application.purchase.candidate_normalizer import ParsedItem, normalize_candidate
from app.common import error_codes
from app.common.enums import PurchaseCandidateSource, PurchaseCandidateStatus
from app.common.exceptions import FriendlyException
from app.infrastructure.importers.link_parser import ProductPage, is_safe_url
from app.infrastructure.importers.router import RoutedLinkParser
from app.infrastructure.persistence.models.purchase_decision import PurchaseCandidate
from app.infrastructure.persistence.repositories.purchase_decision_repo import (
    SqlAlchemyPurchaseCandidateRepository,
)
from app.infrastructure.storage.object_key import is_owned_object_key

logger = structlog.get_logger(__name__)

_LINK_SOURCES = frozenset({PurchaseCandidateSource.SHARE_LINK.value, PurchaseCandidateSource.ASK_LINK.value})
_UPLOAD_SOURCES = frozenset({PurchaseCandidateSource.SCREENSHOT.value, PurchaseCandidateSource.ORDER_IMAGE.value})

# A stored object key -> a short-lived read URL. Injected so tests never need a
# storage backend, and so this module depends on a function rather than on the
# presentation-layer `resolve_render_url` helper (which would invert the layers).
ObjectKeySigner = Callable[[str], str | None]


def sign_object_key(object_key: str) -> str | None:
    """Default signer: the configured object storage, failing soft.

    A signing failure means the provider call cannot happen, which is a parse
    failure (degraded candidate) rather than a request error.
    """
    from app.bootstrap.container import object_storage

    try:
        return object_storage().sign_render_url(object_key)
    except Exception:
        logger.info("v3.purchase_candidate.sign_failed")
        return None


def assert_owned_object_key(object_key: str, user_id: uuid.UUID) -> None:
    """Refuse an object key that is not under this user's upload prefix.

    `POST /v1/uploads/presign` issues keys under `users/<user_id>/` — so a key
    from anywhere else was not issued to this caller. Without this check the
    caller could name any key in the bucket and we would sign it and hand it to
    a vision provider: a cross-user image read. Answered as NOT_FOUND rather
    than FORBIDDEN so the response cannot be used to probe which keys exist
    (contract §3, no existence leak).

    The predicate itself is `is_owned_object_key`, shared with V3-012's avatar
    candidate detection, which needs the same judgement but must answer with a
    no-op rather than a 404 (its host flow is an OOTD save). Only the *response*
    to a foreign key differs between the two surfaces; the rule must not.
    """
    if not is_owned_object_key(object_key, user_id):
        raise FriendlyException(error_codes.NOT_FOUND, "Upload not found", status_code=404)


class IngestPurchaseCandidate:
    def __init__(
        self,
        repository: SqlAlchemyPurchaseCandidateRepository,
        vision: VisionPort,
        ocr: OcrPort,
        link_parser: RoutedLinkParser,
        *,
        signer: ObjectKeySigner = sign_object_key,
    ) -> None:
        self.repository = repository
        self.vision = vision
        self.ocr = ocr
        self.link_parser = link_parser
        self.signer = signer

    async def execute(
        self,
        user_id: uuid.UUID,
        *,
        source_type: str,
        source_url: str | None = None,
        source_object_key: str | None = None,
    ) -> PurchaseCandidate:
        self._validate_source(source_type, source_url, source_object_key)
        if source_object_key is not None:
            assert_owned_object_key(source_object_key, user_id)

        parsed, parsed_ok = await self._parse(
            source_type=source_type,
            source_url=source_url,
            source_object_key=source_object_key,
        )
        status = PurchaseCandidateStatus.PARSED if parsed_ok else PurchaseCandidateStatus.PARSE_FAILED
        logger.info(
            "v3.purchase_candidate.ingested",
            # No URL, object key or page text: §7 forbids logging signed URLs and
            # V3_SPEC §13.5 forbids logging image references.
            source_type=source_type,
            status=status.value,
            has_category=bool(parsed.category),
        )
        return await self.repository.add(
            PurchaseCandidate(
                user_id=user_id,
                source_type=source_type,
                source_url=source_url,
                source_object_key=source_object_key,
                status=status.value,
                parsed_item=parsed.as_payload(),
            )
        )

    @staticmethod
    def _validate_source(source_type: str, source_url: str | None, source_object_key: str | None) -> None:
        """Contract §5.2: exactly one source reference, matching `source_type`."""
        if source_type in _LINK_SOURCES:
            if not source_url or source_object_key:
                raise FriendlyException(
                    error_codes.VALIDATION_ERROR,
                    f"source_url is required for source_type={source_type}",
                    status_code=422,
                )
            if not is_safe_url(source_url):
                raise FriendlyException(error_codes.VALIDATION_ERROR, "Unsafe URL", status_code=400)
        elif source_type in _UPLOAD_SOURCES:
            if not source_object_key or source_url:
                raise FriendlyException(
                    error_codes.VALIDATION_ERROR,
                    f"source_asset_id is required for source_type={source_type}",
                    status_code=422,
                )
        else:  # pragma: no cover - the request schema constrains source_type
            raise FriendlyException(
                error_codes.VALIDATION_ERROR, f"unknown source_type: {source_type}", status_code=422
            )

    async def _parse(
        self,
        *,
        source_type: str,
        source_url: str | None,
        source_object_key: str | None,
    ) -> tuple[ParsedItem, bool]:
        """Normalize the source. Returns (parsed_item, parse_succeeded).

        Never raises for a parse problem — a degraded read is a partial
        `ParsedItem` with `parse_succeeded=False`.
        """
        if source_type == PurchaseCandidateSource.ORDER_IMAGE.value:
            return await self._parse_order_image(source_object_key)
        if source_type == PurchaseCandidateSource.SCREENSHOT.value:
            return await self._parse_screenshot(source_object_key)
        return await self._parse_link(source_url)

    async def _parse_link(self, source_url: str | None) -> tuple[ParsedItem, bool]:
        assert source_url is not None
        page = await self._fetch_page(source_url)
        vision_category: str | None = None
        vision_color: str | None = None
        if page.image_url:
            reading = await self._analyze_image(page.image_url)
            if reading is not None:
                vision_category, vision_color = reading
        # Page text alone still yields material / scene / price hints, so a
        # candidate whose image could not be read is partial, not empty.
        parsed = normalize_candidate(
            category=vision_category,
            color=vision_color,
            raw_text=page.text,
        )
        return parsed, parsed.is_usable

    async def _parse_screenshot(self, source_object_key: str | None) -> tuple[ParsedItem, bool]:
        assert source_object_key is not None
        image_url = self._sign(source_object_key)
        if image_url is None:
            return ParsedItem(), False
        reading = await self._analyze_image(image_url)
        if reading is None:
            return ParsedItem(), False
        category, color = reading
        parsed = normalize_candidate(category=category, color=color)
        return parsed, parsed.is_usable

    async def _parse_order_image(self, source_object_key: str | None) -> tuple[ParsedItem, bool]:
        assert source_object_key is not None
        image_url = self._sign(source_object_key)
        if image_url is None:
            return ParsedItem(), False
        try:
            result = await self.ocr.extract_text(image_url)
        except (ValueError, RuntimeError):
            logger.info("v3.purchase_candidate.ocr_failed")
            return ParsedItem(), False
        # `result.title` is the merchant's product line as OCR read it — raw
        # untrusted text, so it goes through the same bound as page text.
        parsed = normalize_candidate(
            category=result.category,
            color=result.color_main,
            price=result.purchase_price,
            raw_text=result.title,
        )
        return parsed, parsed.is_usable

    async def _fetch_page(self, source_url: str) -> ProductPage:
        try:
            return await self.link_parser.extract_product_page(source_url)
        except Exception:
            # RoutedLinkParser already swallows httpx errors; this is the belt
            # for an unexpected adapter fault, which must degrade not 500.
            logger.info("v3.purchase_candidate.link_parse_failed")
            return ProductPage()

    async def _analyze_image(self, image_url: str) -> tuple[str, str] | None:
        try:
            reading = await self.vision.analyze_image(image_url)
        except (ValueError, RuntimeError):
            logger.info("v3.purchase_candidate.vision_failed")
            return None
        return reading.category, reading.color_main

    def _sign(self, object_key: str) -> str | None:
        """Mint a short-lived read URL for the provider call. Never persisted."""
        return self.signer(object_key)
